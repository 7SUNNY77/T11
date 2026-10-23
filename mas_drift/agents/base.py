"""Общая часть агентов: вызов LLM с записью шага в трассу и счётчики прогона."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mas_drift.infra.llm import LLMClient, LLMResponse
from mas_drift.infra.trace import TraceWriter


@dataclass
class RunStats:
    llm_calls: int = 0        # логические вызовы модели агентами
    api_requests: int = 0     # фактические запросы к провайдеру (с повторами, без кэша)
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0     # с учётом reasoning, если провайдер его сообщает
    llm_latency_s: float = 0.0
    wait_s: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    drift_affected_results: int = 0
    calls_by_agent: dict[str, int] = field(default_factory=dict)

    def add(self, agent: str, r: LLMResponse) -> None:
        self.llm_calls += 1
        self.calls_by_agent[agent] = self.calls_by_agent.get(agent, 0) + 1
        self.api_requests += r.api_requests
        self.cached_calls += int(r.cached)
        self.prompt_tokens += r.usage.prompt_tokens
        self.completion_tokens += r.usage.completion_tokens
        self.reasoning_tokens += r.usage.reasoning_tokens
        self.total_tokens += r.usage.total
        self.llm_latency_s += r.latency_s
        self.wait_s += r.wait_s


@dataclass
class AgentContext:
    llm: LLMClient
    tracer: TraceWriter
    stats: RunStats
    repeat_idx: int = 0


def response_record(r: LLMResponse) -> dict[str, Any]:
    return {
        "response": {"content": r.content, "finish_reason": r.finish_reason},
        "tool_calls": [tc.model_dump() for tc in r.tool_calls],
        "model": r.model,
        "usage": r.usage.model_dump(),
        "latency_s": round(r.latency_s, 4),
        "cached": r.cached,
        "api_requests": r.api_requests,
        "wait_s": round(r.wait_s, 4),
        "message_extra": r.message_extra,
    }


def chat(ctx: AgentContext, agent: str, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
    r = ctx.llm.chat(messages, tools, repeat_idx=ctx.repeat_idx)
    ctx.stats.add(agent, r)
    return r
