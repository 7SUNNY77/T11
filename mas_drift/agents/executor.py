from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Literal

from mas_drift.agents import prompts
from mas_drift.agents.base import AgentContext, chat, response_record
from mas_drift.tools.drift import AnnotatedResult, DriftObservedToolSet
from mas_drift.tools.errors import bad_arguments_json_error, is_error


@dataclass
class ExecutorOutput:
    report: str
    status: Literal["ok", "step_limit"]
    steps: int


def _execute(toolset: DriftObservedToolSet, name: str, raw_args: str) -> tuple[AnnotatedResult, bool]:
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        err = bad_arguments_json_error(raw_args)
        # Ошибка разбора аргументов не зависит от версии API.
        return AnnotatedResult(result=err, drift_affected=False, drift_type="none", shadow_v1_result=err), False
    return toolset.call(name, args), True


def run_executor(
    ctx: AgentContext,
    toolset: DriftObservedToolSet,
    request: str,
    plan: str,
    changelog: str | None,
    max_steps: int,
) -> ExecutorOutput:
    messages: list[dict] = [
        {"role": "system", "content": prompts.system_prompt("executor", changelog)},
        {"role": "user", "content": prompts.executor_user(request, plan)},
    ]
    tools = toolset.schemas()
    for step in range(1, max_steps + 1):
        sent = copy.deepcopy(messages)
        r = chat(ctx, "executor", messages, tools)
        messages.append(r.assistant_message())
        tool_results = []
        for tc in r.tool_calls:
            ann, args_ok = _execute(toolset, tc.name, tc.arguments)
            # Агент видит только result; разметка дрейфа идёт лишь в трассу.
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(ann.result, ensure_ascii=False)})
            ctx.stats.tool_calls += 1
            ctx.stats.tool_errors += int(is_error(ann.result))
            ctx.stats.drift_affected_results += int(ann.drift_affected)
            tool_results.append({"tool_call_id": tc.id, "name": tc.name, "arguments": tc.arguments,
                                 "arguments_parsed": args_ok, **ann.model_dump()})
        ctx.tracer.step("executor", executor_step=step, messages=sent, **response_record(r),
                        tool_results=tool_results)
        if not r.tool_calls:
            return ExecutorOutput(report=r.content or "", status="ok", steps=step)
    return ExecutorOutput(report=prompts.EXECUTOR_STEP_LIMIT_REPORT, status="step_limit", steps=max_steps)
