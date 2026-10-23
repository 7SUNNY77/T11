import json
import sys
from pathlib import Path

import pytest

from mas_drift.agents.policy_fake import PolicyFake
from mas_drift.agents.prompts import CHANGELOG_HEADER, SYSTEM_PROMPTS
from mas_drift.infra.config import DEFAULT_CONFIG, ProviderConfig
from mas_drift.infra.fake import ScriptedFake, text
from mas_drift.infra.llm import build_llm
from mas_drift.infra.ratelimit import RateLimitedLLM, RateLimiter
from mas_drift.infra.trace import read_trace
from mas_drift.judge.compress import MAX_TEXT, VIEWS, compress_trace, leaks
from mas_drift.judge.prompt import JUDGE_SYSTEM, judge_system, parse_verdict
from mas_drift.judge.run import judge_one, judge_setup, score
from mas_drift.judge.truth import ground_truth
from mas_drift.runner import cli
from mas_drift.tools.changelogs import CHANGELOGS, IRRELEVANT_CHANGELOG
from mas_drift.tools.versions import API_VERSIONS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

EXP = "fake-leakzeta"


@pytest.fixture(scope="module")
def traces(tmp_path_factory) -> dict[str, list[dict]]:
    """Настоящие трассы пайплайна на fake: 3 задачи × все 10 условий."""
    tmp = tmp_path_factory.mktemp("judge")
    cfg = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        'runs_dir = "runs"', f'runs_dir = "{(tmp / "runs").as_posix()}"')
    path = tmp / "c.toml"
    path.write_text(cfg, encoding="utf-8")
    llm = build_llm(ProviderConfig(name="fake", kind="fake", model="policy-fake", cache=False), None, raw=PolicyFake())
    cli.main(["--config", str(path), "--provider", "fake", "--experiment", EXP, "--tasks", "T01,T04,T05"],
             llm=llm, log=lambda s: None)
    out = {}
    for p in sorted((tmp / "runs" / EXP / "fake" / "traces").glob("*.jsonl")):
        out[p.stem] = read_trace(p)
    assert len(out) == 30
    return out


def _forbidden_explicit() -> list[str]:
    conds = [f"{v}:{a}" for v in API_VERSIONS for a in ("A0", "A1")]
    lines = [ln.strip() for t in [*SYSTEM_PROMPTS.values(), *CHANGELOGS.values(), IRRELEVANT_CHANGELOG]
             for ln in t.splitlines() if len(ln.strip()) >= 20]
    return [*conds, *API_VERSIONS[1:], "drift_affected", "drift_type", "drift", "shadow_v1_result", "shadow",
            EXP, CHANGELOG_HEADER, "changelog", "Changelog", "You are the", *lines]


@pytest.mark.parametrize("view", VIEWS)
def test_judge_input_has_no_forbidden_strings(traces, view):
    forbidden = _forbidden_explicit()
    for run_id, recs in traces.items():
        txt = compress_trace(recs, view).text
        hits = [s for s in forbidden if s in txt]
        assert hits == [], (run_id, hits)
        assert leaks(txt, recs) == [], run_id
        # Системные промпты агентов (в A1 — с changelog'ом) целиком.
        for r in recs:
            if r.get("type") == "step":
                assert r["messages"][0]["content"] not in txt


@pytest.mark.parametrize("view", VIEWS)
def test_judge_system_prompt_is_condition_blind(view):
    for s in [*API_VERSIONS[1:], "drift", "changelog", "A0", "A1", "cent", "num_nights", "schema"]:
        assert s not in judge_system(view), s
    assert ("ответы инструментов НЕ показаны" in judge_system(view)) == (view == "messages_only")
    assert judge_system("full") == JUDGE_SYSTEM


def test_messages_only_hides_tool_responses(traces):
    for cond in ("v2_units:A0", "v2_contract_stale:A0"):
        recs = _by_cond(traces, cond)
        full, mo = compress_trace(recs, "full").text, compress_trace(recs, "messages_only").text
        assert "ответ: список" in full or "ОШИБКА" in full
        # Ответ инструмента в full печатается строкой «    ответ: …» под вызовом.
        assert "\n    ответ:" not in mo and "ОШИБКА" not in mo and "Unprocessable" not in mo
        assert '"name":' not in mo  # элементы ответов search_hotels не протекают
        # Вызовы с аргументами, отчёт executor и ответ verifier остаются.
        assert 'search_hotels({"city"' in mo and "отчёт для verifier" in mo and "verifier → ответ" in mo
        assert compress_trace(recs, "messages_only").steps == compress_trace(recs, "full").steps
    with pytest.raises(ValueError):
        compress_trace(recs, "everything")


def test_leak_detector_catches_and_word_boundary(traces):
    recs = next(iter(traces.values()))
    assert "drift_affected" in leaks("... drift_affected ...", recs)
    exp_like = dict(recs[0], experiment="main")
    assert leaks("remaining domain", [exp_like]) == [] and leaks("exp main here", [exp_like]) == ["main"]


def _by_cond(traces, cond, task="T01"):
    return next(r for k, r in traces.items() if k.startswith(cond.replace(":", "_") + f"_{task}_"))


def test_compression_format(traces):
    recs = _by_cond(traces, "v2_units:A0")
    c = compress_trace(recs)
    assert c.steps == [r["step"] for r in recs if r.get("type") == "step"]
    for n in c.steps:
        assert f"[шаг {n}]" in c.text
    assert "ЭТАЛОННЫЙ ОТВЕТ" in c.text and "прогон провален" in c.text and "ФИНАЛЬНЫЙ ОТВЕТ СИСТЕМЫ" in c.text
    # Первые 5 элементов со всеми полями, цены в центах видны как есть.
    assert "показаны первые 5" in c.text
    hotel_lines = [ln for ln in c.text.splitlines() if ln.strip().startswith('{"id": "H')]
    assert len(hotel_lines) == 5 and all('"name"' in ln and '"city"' in ln and '"price"' in ln for ln in hotel_lines)
    assert all(isinstance(json.loads(ln.strip())["price"], int) for ln in hotel_lines)
    stale = compress_trace(_by_cond(traces, "v2_contract_stale:A0")).text
    assert 'ОШИБКА {"error": "Unprocessable Entity", "status": 422' in stale and '"extra_forbidden"' in stale
    assert c.est_tokens < 3000


def test_compression_truncates_long_text(traces):
    recs = json.loads(json.dumps(_by_cond(traces, "v1:A0")))
    long = "x" * 5000
    for r in recs:
        if r.get("agent") == "planner":
            r["response"]["content"] = long
    txt = compress_trace(recs).text
    assert "x" * MAX_TEXT in txt and "x" * (MAX_TEXT + 1) not in txt and "урезано, всего 5000" in txt


def test_ground_truth(traces):
    # Отклонённый вызов: агент ошибся на том же шаге, где отправил вызов.
    assert ground_truth(_by_cond(traces, "v2_contract_stale:A0")) == {
        "status": "ok", "agent": "executor", "drift_entry_step": 2, "agent_error_step": 2,
        "rule": "rejected_call", "trigger": "output_renamed+tool_error"}
    # Изменённый выход: дрейф вошёл на шаге 2, агент действовал на нём на следующем шаге executor.
    t = ground_truth(_by_cond(traces, "v2_units:A0"))
    assert (t["drift_entry_step"], t["agent_error_step"], t["rule"], t["trigger"]) == (
        2, 3, "next_executor_step", "output_units")
    assert ground_truth(_by_cond(traces, "v1:A0"))["status"] == "no_ground_truth"
    assert ground_truth(_by_cond(traces, "v2_benign:A0"))["status"] == "no_ground_truth"


@pytest.mark.parametrize("raw,ok", [
    ('{"blamed_agent":"executor","blamed_step":2,"error_category":"misread_tool_output","root_cause":"x","confidence":0.8}', True),
    ('```json\n{"blamed_agent":"executor","blamed_step":2,"error_category":"other","root_cause":"x","confidence":1}\n```', True),
    ('{"blamed_agent":"judge","blamed_step":2,"error_category":"other","root_cause":"x","confidence":0.5}', False),
    ('{"blamed_agent":"executor","blamed_step":2,"error_category":"other","root_cause":"x","confidence":1.5}', False),
    ("not json", False), (None, False),
])
def test_parse_verdict(raw, ok):
    v, err = parse_verdict(raw)
    assert (v is not None) == ok and (err is None) == ok


def _res(api, agent, step, cause, error_step=3, entry_step=2, truth=True):
    return {"api_version": api, "awareness": "A0", "usage": {"prompt_tokens": 1000},
            "truth": {"status": "ok", "agent": "executor", "agent_error_step": error_step,
                      "drift_entry_step": entry_step} if truth else {"status": "no_ground_truth"},
            "verdict": None if agent is None else {"blamed_agent": agent, "blamed_step": step, "confidence": 0.5,
                                                   "error_category": "other", "root_cause": cause}}


def test_score_metrics():
    results = [
        _res("v2_units", "executor", 2, "Prices were in cents."),     # шаг входа точно, шаг ошибки ±1
        _res("v2_units", "executor", 3, "Executor misread values."),  # шаг ошибки точно
        _res("v2_units", "verifier", 4, "Verifier wrongly said no option."),
        _res("v2_units", None, 0, ""),
        _res("v2_contract_stale", "executor", 2, "Неверное имя параметра num_nights.", error_step=2),
        _res("v1", "executor", 2, "x", truth=False),
    ]
    by = {s.group: s for s in score(results)}
    u = by["v2_units:A0"]
    assert (u.n, u.n_truth, u.parse_fail) == (4, 4, 1)
    assert (u.agent_ok, u.step_exact, u.step_pm1, u.downstream, u.blamed_verifier) == (2, 1, 2, 1, 1)
    assert (u.entry_exact, u.entry_pm1) == (1, 2)
    assert (u.cause_ok, u.cause_n) == (1, 3)
    assert by["v2_contract_stale:A0"].cause_ok == 1
    assert by["v1:A0"].n_truth == 0 and by["ВСЕ"].n == 6 and by["ВСЕ"].n_truth == 5


def test_tpm_limiter_reserves_completion():
    class Clock:
        t = 0.0
        def __call__(self): return self.t
        def sleep(self, s): self.t += s

    c = Clock()
    lim = RateLimiter(tpm=8000, clock=c, sleep=c.sleep)
    big = [{"role": "user", "content": "x" * 12000}]  # ~3000 токенов
    llm = RateLimitedLLM(ScriptedFake([text("a"), text("b")]), lim, est_completion=1500)
    llm.chat(big)
    lim._tokens[-1] = (lim._tokens[-1][0], 4500)  # фактический расход с рассуждениями
    r = llm.chat(big)
    assert r.wait_s == pytest.approx(60)  # 4500 + (3000 + 1500) > 8000 → ждём окно


def test_judge_one_and_reports(traces, tmp_path, monkeypatch):
    import judge as judge_cli

    recs = _by_cond(traces, "v2_contract_stale:A0")
    trace_file = tmp_path / f"{recs[0]['run_id']}.jsonl"
    trace_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    pcfg = ProviderConfig(name="groq", kind="openai_compat", model="judge-x", temperature=0.0,
                          reasoning_effort="low", json_mode=True)
    answer = ('{"blamed_agent":"executor","blamed_step":2,"error_category":"wrong_tool_call",'
              '"root_cause":"Executor передаёт nights вместо num_nights.","confidence":0.9}')
    fake = ScriptedFake([text(answer)])
    by_view = {}
    for view in VIEWS:
        fake = ScriptedFake([text(answer)])
        res = judge_one(trace_file, fake, judge_setup(pcfg, view))
        sent = fake.calls[0]["messages"]
        assert sent[0]["content"] == judge_system(view) and sent[1]["content"] == res["input_text"]
        assert res["view"] == view and res["judge"]["view"] == view and res["input_leaks"] == []
        assert res["verdict"]["blamed_step"] == 2 and res["truth"]["agent_error_step"] == 2
        assert res["judge"]["temperature"] == 0.0 and res["judge"]["reasoning_effort"] == "low"
        by_view[view] = [res]
    setups = {v: rs[0]["judge"] for v, rs in by_view.items()}
    judge_cli.write_reports(by_view, tmp_path / "rep", setups, {v: v for v in VIEWS}, 1, seed=7)
    md = (tmp_path / "rep" / "judge.md").read_text(encoding="utf-8")
    assert "temperature = 0.0" in md and "1/1 (100%)" in md
    assert "## Режим `full`" in md and "## Режим `messages_only`" in md and "Винит verifier" in md
    assert "judge_results.csv" in [p.name for p in (tmp_path / "rep").iterdir()]
    assert recs[0]["run_id"] in (tmp_path / "rep" / "judge_samples.md").read_text(encoding="utf-8")


def test_step_limit_note_before_verifier(traces):
    recs = json.loads(json.dumps(_by_cond(traces, "v1:A0")))
    recs[-1]["executor_status"] = "step_limit"
    txt = compress_trace(recs).text
    assert txt.index("[система] executor исчерпал лимит шагов") < txt.index("verifier → ответ")
