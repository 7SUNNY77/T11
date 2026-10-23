"""Скриптованный fake-провайдер для юнит-тестов.

Правиловый PolicyFake для сквозного прогона лежит в mas_drift/agents/policy_fake.py:
он завязан на соглашения промптов агентов.
"""

from __future__ import annotations

import copy
import json
from typing import Callable, Union

from mas_drift.infra.llm import LLMClient, LLMResponse, ToolCall, Usage, estimate_tokens

ScriptItem = Union[LLMResponse, Exception, Callable[[list[dict], list[dict] | None], LLMResponse]]


def fake_usage(messages: list[dict], tools: list[dict] | None, response: LLMResponse) -> Usage:
    out = (response.content or "") + json.dumps([tc.model_dump() for tc in response.tool_calls])
    return Usage(prompt_tokens=estimate_tokens(messages, tools), completion_tokens=len(out) // 4)


def text(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def tool_calls(*calls: tuple[str, dict]) -> LLMResponse:
    return LLMResponse(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=json.dumps(args))
            for i, (name, args) in enumerate(calls)
        ],
    )


class ScriptedFake(LLMClient):
    def __init__(self, script: list[ScriptItem], provider: str = "fake", model: str = "scripted"):
        self.provider, self.model, self.temperature = provider, model, 0.0
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "repeat_idx": repeat_idx})
        if not self.script:
            raise AssertionError("ScriptedFake: сценарий закончился")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        response = item(messages, tools) if callable(item) else item.model_copy(deep=True)
        response.model = self.model
        response.usage = fake_usage(messages, tools, response)
        return response
