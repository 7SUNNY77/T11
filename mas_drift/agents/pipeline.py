"""Последовательная МАС: Planner → Executor → Verifier."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from mas_drift.agents.base import AgentContext, RunStats
from mas_drift.agents.executor import run_executor
from mas_drift.agents.planner import run_planner
from mas_drift.agents.verifier import run_verifier
from mas_drift.domain.tasks import Task
from mas_drift.domain.world import World
from mas_drift.infra.llm import LLMClient
from mas_drift.infra.trace import TraceWriter
from mas_drift.tools.changelogs import changelog_for
from mas_drift.tools.drift import DriftObservedToolSet


class Condition(BaseModel):
    api_version: str
    awareness: str
    changelog_to: list[str]  # агенты, получающие changelog текущей версии

    @property
    def id(self) -> str:
        return f"{self.api_version}:{self.awareness}"

    def changelog_for_agent(self, agent: str) -> str | None:
        return changelog_for(self.api_version) if agent in self.changelog_to else None


@dataclass
class MASResult:
    final_text: str
    executor_status: str
    executor_steps: int
    stats: RunStats


def run_mas(
    task: Task,
    condition: Condition,
    world: World,
    llm: LLMClient,
    tracer: TraceWriter,
    repeat_idx: int,
    executor_max_steps: int = 8,
) -> MASResult:
    ctx = AgentContext(llm=llm, tracer=tracer, stats=RunStats(), repeat_idx=repeat_idx)
    toolset = DriftObservedToolSet.for_version(condition.api_version, world)
    plan = run_planner(ctx, task.request, condition.changelog_for_agent("planner"))
    ex = run_executor(ctx, toolset, task.request, plan, condition.changelog_for_agent("executor"),
                      executor_max_steps)
    final = run_verifier(ctx, task.request, plan, ex.report, condition.changelog_for_agent("verifier"))
    return MASResult(final_text=final, executor_status=ex.status, executor_steps=ex.steps, stats=ctx.stats)
