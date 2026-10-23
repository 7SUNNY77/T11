import json

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from mas_drift.infra.cache import CachedLLM, DiskCache, cache_key
from mas_drift.infra.config import ProviderConfig
from mas_drift.infra.errors import DailyQuotaExceeded, LLMRequestError, RetriesExhausted, RetryableLLMError
from mas_drift.infra.fake import ScriptedFake, text, tool_calls
from mas_drift.infra.llm import OpenAICompatClient, classify_openai_error
from mas_drift.infra.ratelimit import RateLimitedLLM, RateLimiter, RetryingLLM
from mas_drift.infra.trace import TraceWriter, is_completed, read_trace, trace_path

MSGS = [{"role": "user", "content": "hi"}]


# --- cache ---

def test_cache_key_components():
    base = dict(provider="p", model="m", messages=MSGS, tools=None, temperature=0.7, repeat_idx=0)
    k = cache_key(**base)
    assert k == cache_key(**base)
    for change in ({"repeat_idx": 1}, {"temperature": 0.0}, {"model": "m2"}, {"provider": "q"},
                   {"messages": [{"role": "user", "content": "hi!"}]}, {"tools": [{"x": 1}]}):
        assert cache_key(**{**base, **change}) != k, change


def test_cached_llm_hit_and_repeat_idx(tmp_path):
    fake = ScriptedFake([text("a"), text("b"), text("c")])
    llm = CachedLLM(fake, DiskCache(tmp_path))
    r1 = llm.chat(MSGS, repeat_idx=0)
    r2 = llm.chat(MSGS, repeat_idx=0)
    r3 = llm.chat(MSGS, repeat_idx=1)
    assert (r1.content, r2.content, r3.content) == ("a", "a", "b")
    assert not r1.cached and r2.cached and r2.api_requests == 0 and not r3.cached
    assert len(fake.calls) == 2
    # Новый экземпляр видит тот же кэш на диске.
    fresh = CachedLLM(ScriptedFake([]), DiskCache(tmp_path))
    assert fresh.chat(MSGS, repeat_idx=1).content == "b"


# --- rate limiter ---

class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_rpm_limit():
    c = Clock()
    lim = RateLimiter(rpm=2, clock=c, sleep=c.sleep)
    assert lim.acquire() == 0 and lim.acquire() == 0
    c.t = 10
    assert lim.acquire() == pytest.approx(50)
    assert c.t == pytest.approx(60)


def test_tpm_limit():
    c = Clock()
    lim = RateLimiter(tpm=1000, clock=c, sleep=c.sleep)
    lim.acquire(600)
    lim.record_tokens(800)
    c.t = 5
    assert lim.acquire(300) == pytest.approx(55)
    # Одиночный запрос больше лимита не блокируется навсегда.
    lim2 = RateLimiter(tpm=100, clock=c, sleep=c.sleep)
    assert lim2.acquire(5000) == 0


def test_rate_limited_wrapper_records_wait():
    c = Clock()
    llm = RateLimitedLLM(ScriptedFake([text("a"), text("b")]), RateLimiter(rpm=1, clock=c, sleep=c.sleep))
    assert llm.chat(MSGS).wait_s == 0
    assert llm.chat(MSGS).wait_s == pytest.approx(60)


# --- retry ---

def _retrying(script, **kw):
    sleeps = []
    llm = RetryingLLM(ScriptedFake(script), sleep=sleeps.append, rand=lambda: 1.0, **kw)
    return llm, sleeps


def test_retry_exponential_then_success():
    llm, sleeps = _retrying([RetryableLLMError("429", 429), RetryableLLMError("503", 503), text("ok")],
                            base_s=2, max_s=100)
    r = llm.chat(MSGS)
    assert r.content == "ok" and r.api_requests == 3
    assert sleeps == [2, 4] and r.wait_s == 6


def test_retry_after_honored_and_capped():
    llm, sleeps = _retrying([RetryableLLMError("429", 429, retry_after=7), RetryableLLMError("x", retry_after=999),
                             text("ok")], max_s=30)
    llm.chat(MSGS)
    assert sleeps == [7, 30]


def test_retries_exhausted():
    llm, sleeps = _retrying([RetryableLLMError("x")] * 3, max_retries=2)
    with pytest.raises(RetriesExhausted):
        llm.chat(MSGS)
    assert len(sleeps) == 2


def test_daily_quota_not_retried():
    llm, sleeps = _retrying([DailyQuotaExceeded("day")])
    with pytest.raises(DailyQuotaExceeded):
        llm.chat(MSGS)
    assert sleeps == []


# --- openai error mapping ---

def _resp(status, headers=None):
    return httpx2.Response(status, request=httpx2.Request("POST", "http://x/v1/chat/completions"), headers=headers)


def test_classify_openai_errors():
    e = classify_openai_error(openai.RateLimitError("Too many requests", response=_resp(429, {"retry-after": "7"}), body=None))
    assert isinstance(e, RetryableLLMError) and e.status == 429 and e.retry_after == 7
    gemini_daily = openai.RateLimitError(
        "Error code: 429", response=_resp(429),
        body={"error": {"message": "Quota exceeded for metric: generate_requests_per_model_per_day"}},
    )
    assert isinstance(classify_openai_error(gemini_daily), DailyQuotaExceeded)
    groq_daily = openai.RateLimitError("Rate limit reached on tokens per day (TPD)", response=_resp(429), body=None)
    assert isinstance(classify_openai_error(groq_daily), DailyQuotaExceeded)
    gemini_minute = openai.RateLimitError(
        "Error code: 429", response=_resp(429),
        body={"error": {"details": [{"@type": "RetryInfo", "retryDelay": "36s"}]}},
    )
    e = classify_openai_error(gemini_minute)
    assert isinstance(e, RetryableLLMError) and e.retry_after == 36
    e = classify_openai_error(openai.InternalServerError("boom", response=_resp(503), body=None))
    assert isinstance(e, RetryableLLMError) and e.status == 503
    e = classify_openai_error(openai.APIConnectionError(request=httpx2.Request("POST", "http://x")))
    assert isinstance(e, RetryableLLMError)
    e = classify_openai_error(openai.BadRequestError("bad", response=_resp(400), body=None))
    assert isinstance(e, LLMRequestError)


def test_openai_client_parses_tool_calls(monkeypatch):
    monkeypatch.setenv("MASX_KEY", "k")
    cfg = ProviderConfig(name="p", kind="openai_compat", model="m", base_url="http://localhost:1/v1",
                         api_key_env="MASX_KEY")
    client = OpenAICompatClient(cfg)
    completion = ChatCompletion.model_validate({
        "id": "1", "object": "chat.completion", "created": 0, "model": "m-001",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "", "type": "function",
                            "function": {"name": "search_hotels", "arguments": '{"city":"X","nights":2}'}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    })
    seen = {}

    def create(**kw):
        seen.update(kw)
        return completion

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    r = client.chat(MSGS, tools=[{"type": "function"}])
    assert seen["tools"] == [{"type": "function"}] and seen["temperature"] == 0.0
    assert r.tool_calls[0].id == "call_0" and r.tool_calls[0].name == "search_hotels"
    assert r.usage.prompt_tokens == 11 and r.model == "m-001"
    assert r.assistant_message()["tool_calls"][0]["function"]["arguments"] == '{"city":"X","nights":2}'

    def fail(**kw):
        raise openai.RateLimitError("x per day", response=_resp(429), body=None)

    monkeypatch.setattr(client._client.chat.completions, "create", fail)
    with pytest.raises(DailyQuotaExceeded):
        client.chat(MSGS)


def test_scripted_fake_helpers():
    r = ScriptedFake([tool_calls(("a", {"x": 1}), ("b", {}))]).chat(MSGS)
    assert [tc.name for tc in r.tool_calls] == ["a", "b"] and json.loads(r.tool_calls[0].arguments) == {"x": 1}
    assert r.usage.prompt_tokens > 0 and r.usage.completion_tokens > 0


# --- traces ---

def test_trace_finish_is_completed(tmp_path):
    path = trace_path(tmp_path, "run1")
    with TraceWriter(path, {"run_id": "run1"}) as tw:
        tw.step("planner", messages=MSGS, response={"content": "x"})
        assert not is_completed(path)
        tw.finish(success=True)
    assert is_completed(path)
    recs = read_trace(path)
    assert [r["type"] for r in recs] == ["run_start", "step", "run_end"]
    assert recs[1]["step"] == 1 and recs[1]["agent"] == "planner"
    assert not path.with_name("run1.jsonl.part").exists()


def test_trace_abort_on_exception_and_rerun(tmp_path):
    path = trace_path(tmp_path, "run2")
    with pytest.raises(DailyQuotaExceeded):
        with TraceWriter(path, {"run_id": "run2"}) as tw:
            tw.step("planner")
            raise DailyQuotaExceeded("quota")
    part = path.with_name("run2.jsonl.part")
    assert not is_completed(path) and part.exists()
    assert read_trace(part)[-1]["type"] == "run_aborted"
    with TraceWriter(path, {"run_id": "run2"}) as tw:
        tw.finish()
    assert is_completed(path) and not part.exists()
