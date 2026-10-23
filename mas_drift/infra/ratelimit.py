"""Rate limiter (RPM и TPM в скользящем окне 60 с) и retry с экспоненциальной паузой."""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Callable

from mas_drift.infra.errors import RetriesExhausted, RetryableLLMError
from mas_drift.infra.llm import LLMClient, LLMResponse, LLMWrapper, estimate_tokens

WINDOW_S = 60.0


class RateLimiter:
    def __init__(
        self,
        rpm: int = 0,
        tpm: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rpm, self.tpm = rpm, tpm
        self.clock, self.sleep = clock, sleep
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int]] = deque()
        # Один лимитер на все воркеры: состояние меняется только под замком, спим вне его.
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        while self._requests and now - self._requests[0] >= WINDOW_S:
            self._requests.popleft()
        while self._tokens and now - self._tokens[0][0] >= WINDOW_S:
            self._tokens.popleft()

    def acquire(self, est_tokens: int = 0) -> float:
        """Блокирует до появления квоты; возвращает время ожидания."""
        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                self._purge(now)
                wait = 0.0
                if self.rpm and len(self._requests) >= self.rpm:
                    wait = self._requests[0] + WINDOW_S - now
                if self.tpm and self._tokens and sum(n for _, n in self._tokens) + est_tokens > self.tpm:
                    wait = max(wait, self._tokens[0][0] + WINDOW_S - now)
                if wait <= 0:
                    self._requests.append(now)
                    return waited
            self.sleep(wait)
            waited += wait

    def record_tokens(self, n: int) -> None:
        if self.tpm and n:
            with self._lock:
                self._tokens.append((self.clock(), n))


class RateLimitedLLM(LLMWrapper):
    def __init__(self, inner: LLMClient, limiter: RateLimiter, est_completion: int = 0):
        super().__init__(inner)
        self.limiter = limiter
        self.est_completion = est_completion

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        waited = self.limiter.acquire(estimate_tokens(messages, tools) + self.est_completion)
        response = self.inner.chat(messages, tools, repeat_idx=repeat_idx)
        self.limiter.record_tokens(response.usage.total)
        response.wait_s += waited
        return response


class RetryingLLM(LLMWrapper):
    def __init__(
        self,
        inner: LLMClient,
        max_retries: int = 6,
        base_s: float = 2.0,
        max_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ):
        super().__init__(inner)
        self.max_retries, self.base_s, self.max_s = max_retries, base_s, max_s
        self.sleep, self.rand = sleep, rand

    def backoff(self, attempt: int, error: RetryableLLMError) -> float:
        if error.retry_after is not None:
            return min(self.max_s, error.retry_after)
        # Экспонента с jitter в [0.5, 1.0] от номинала.
        return min(self.max_s, self.base_s * 2**attempt) * (0.5 + 0.5 * self.rand())

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        slept = 0.0
        for attempt in range(self.max_retries + 1):
            try:
                response = self.inner.chat(messages, tools, repeat_idx=repeat_idx)
            except RetryableLLMError as exc:
                if attempt == self.max_retries:
                    raise RetriesExhausted(f"{attempt + 1} попыток, последняя ошибка: {exc}") from exc
                delay = self.backoff(attempt, exc)
                self.sleep(delay)
                slept += delay
                continue
            response.api_requests = attempt + 1
            response.wait_s += slept
            return response
        raise AssertionError("unreachable")
