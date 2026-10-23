from __future__ import annotations

import json

from mas_drift.agents import prompts
from mas_drift.agents.base import AgentContext, chat, response_record
from mas_drift.agents.jsonutil import extract_json


def run_planner(ctx: AgentContext, request: str, changelog: str | None) -> str:
    """Возвращает план как текст для следующих агентов (нормализованный JSON, если разобрался)."""
    messages = [
        {"role": "system", "content": prompts.system_prompt("planner", changelog)},
        {"role": "user", "content": request},
    ]
    r = chat(ctx, "planner", messages)
    plan = extract_json(r.content)
    parsed = isinstance(plan, dict)
    ctx.tracer.step("planner", messages=messages, **response_record(r), tool_results=[], plan_parsed=parsed)
    return json.dumps(plan, ensure_ascii=False) if parsed else (r.content or "")
