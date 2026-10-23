"""LLM-клиенты: общий интерфейс, OpenAI-совместимый клиент и сборка стека обёрток."""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mas_drift.infra.config import ProviderConfig
from mas_drift.infra.errors import DailyQuotaExceeded, LLMRequestError, RetryableLLMError


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # сырой JSON-текст, как его выдала модель
    # Нестандартные поля провайдера внутри tool_call (у Gemini 3 — extra_content с thought_signature).
    # Возвращаются в историю дословно: без них Gemini отвечает 400 на следующем шаге.
    extra: dict[str, Any] = {}


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0              # если провайдер сообщает их отдельно
    provider_total_tokens: int | None = None  # total_tokens от провайдера (может включать reasoning)

    @property
    def total(self) -> int:
        summed = self.prompt_tokens + self.completion_tokens
        return max(summed, self.provider_total_tokens or 0)


class LLMResponse(BaseModel):
    content: str | None
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: Usage = Usage()
    model: str = ""
    latency_s: float = 0.0
    cached: bool = False
    api_requests: int = 1  # фактические запросы к провайдеру, включая повторы; 0 при попадании в кэш
    wait_s: float = 0.0    # ожидание rate limiter'а и пауз retry
    # Нестандартные поля сообщения — только для трассы. В историю НЕ возвращаются:
    # часть провайдеров (например, Groq с полем reasoning) отвергает их во входных сообщениях.
    message_extra: dict[str, Any] = {}

    def assistant_message(self) -> dict:
        """Сообщение ассистента для истории диалога в формате OpenAI."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments},
                 **tc.extra}
                for tc in self.tool_calls
            ]
        return msg


class LLMClient(ABC):
    provider: str
    model: str
    temperature: float
    # Прочие параметры генерации (например, reasoning_effort); входят в ключ кэша.
    extra_params: dict[str, Any] = {}

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        ...


class LLMWrapper(LLMClient):
    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.provider, self.model, self.temperature = inner.provider, inner.model, inner.temperature
        self.extra_params = inner.extra_params


def estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Оценка до вызова: ~4 символа на токен для ASCII, ~2 для прочего (кириллица дороже)."""
    s = json.dumps([messages, tools or []], ensure_ascii=False)
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    return (len(s) - non_ascii) // 4 + non_ascii // 2


# --- OpenAI-совместимый клиент (gemini / groq / ollama) ---

_DAILY = re.compile(r"per[ _-]?day|perday|\bRPD\b|\bTPD\b|daily", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retry[_ ]?delay\W+(\d+(?:\.\d+)?)s", re.IGNORECASE)


def _error_text(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    return f"{exc} {json.dumps(body, ensure_ascii=False, default=str) if body is not None else ''}"


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    if response is not None:
        value = response.headers.get("retry-after")
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    m = _RETRY_DELAY.search(_error_text(exc))
    return float(m.group(1)) if m else None


def classify_openai_error(exc: Exception) -> Exception:
    import openai

    text = _error_text(exc)
    if isinstance(exc, openai.RateLimitError):
        if _DAILY.search(text):
            return DailyQuotaExceeded(text)
        return RetryableLLMError(text, status=429, retry_after=_retry_after(exc))
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return RetryableLLMError(text)
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 408:
            return RetryableLLMError(text, status=exc.status_code, retry_after=_retry_after(exc))
        return LLMRequestError(f"HTTP {exc.status_code}: {text}")
    return exc


class OpenAICompatClient(LLMClient):
    def __init__(self, cfg: ProviderConfig):
        import openai

        self.provider, self.model, self.temperature = cfg.name, cfg.model, cfg.temperature
        self.extra_params = {}
        if cfg.reasoning_effort:
            self.extra_params["reasoning_effort"] = cfg.reasoning_effort
        if cfg.json_mode:
            self.extra_params["response_format"] = {"type": "json_object"}
        # Повторы делает RetryingLLM, у SDK они отключены, чтобы считать запросы самим.
        self._client = openai.OpenAI(
            base_url=cfg.base_url, api_key=cfg.api_key(), max_retries=0, timeout=cfg.timeout_s
        )

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        kwargs: dict[str, Any] = dict(model=self.model, messages=messages, temperature=self.temperature,
                                      **self.extra_params)
        if tools:
            kwargs["tools"] = tools
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 — переводим в наши типы
            mapped = classify_openai_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        latency = time.perf_counter() - t0
        return self.parse_response(resp, latency)

    @staticmethod
    def parse_response(resp: Any, latency: float) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(
                id=tc.id or f"call_{i}",
                name=tc.function.name,
                arguments=tc.function.arguments or "{}",
                extra=dict(tc.model_extra or {}),
            )
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        usage = resp.usage
        details = getattr(usage, "completion_tokens_details", None)
        return LLMResponse(
            content=msg.content,
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
                provider_total_tokens=getattr(usage, "total_tokens", None),
            ),
            model=resp.model or "",
            latency_s=latency,
            message_extra=dict(msg.model_extra or {}),
        )


def build_llm(cfg: ProviderConfig, cache_dir: Path | None, raw: LLMClient | None = None) -> LLMClient:
    """Стек: кэш → retry → rate limiter → клиент. Кэш снаружи, чтобы попадания не тратили лимит."""
    from mas_drift.infra.cache import CachedLLM, DiskCache
    from mas_drift.infra.ratelimit import RateLimitedLLM, RateLimiter, RetryingLLM

    if raw is None:
        if cfg.kind == "fake":
            from mas_drift.agents.policy_fake import PolicyFake

            raw = PolicyFake(model=cfg.model)
        else:
            raw = OpenAICompatClient(cfg)
    llm: LLMClient = RateLimitedLLM(raw, RateLimiter(rpm=cfg.rpm, tpm=cfg.tpm), est_completion=cfg.est_completion_tokens)
    llm = RetryingLLM(llm, max_retries=cfg.max_retries, base_s=cfg.backoff_base_s, max_s=cfg.backoff_max_s)
    if cfg.cache and cache_dir is not None:
        llm = CachedLLM(llm, DiskCache(cache_dir))
    return llm
