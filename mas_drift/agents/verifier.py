from __future__ import annotations

from mas_drift.agents import prompts
from mas_drift.agents.base import AgentContext, chat, response_record


def run_verifier(ctx: AgentContext, request: str, plan: str, report: str, changelog: str | None) -> str:
    """Возвращает сырой текст финального ответа; его разбирает и оценивает eval/checker."""
    messages = [
        {"role": "system", "content": prompts.system_prompt("verifier", changelog)},
        {"role": "user", "content": prompts.verifier_user(request, plan, report)},
    ]
    r = chat(ctx, "verifier", messages)
    ctx.tracer.step("verifier", messages=messages, **response_record(r), tool_results=[])
    return r.content or ""
