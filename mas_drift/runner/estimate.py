"""Оценка бюджета сетки: LLM-вызовы, токены, время с учётом лимитов, воркеров и реальной задержки.

Источник замеров (по приоритету): завершённые трассы этого эксперимента → завершённые smoke-трассы
того же провайдера и модели → априорные значения (если реальных трасс ещё нет).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mas_drift.infra.config import ProviderConfig
from mas_drift.infra.trace import is_completed, read_trace, trace_path
from mas_drift.runner.grid import RunSpec

PRIOR = {"calls_per_run": 4.0, "planner_calls_per_run": 1.0, "tokens_per_call": 900.0, "latency_per_call_s": 20.0}


@dataclass
class Measured:
    source: str
    runs: int
    calls_per_run: float
    planner_calls_per_run: float
    tokens_per_call: float
    latency_per_call_s: float  # только по реальным (не кэшированным) запросам


def measure(paths: list[Path], source: str, match: dict | None = None) -> Measured | None:
    """match — поля run_start, которые должны совпасть (например model, reasoning_effort)."""
    runs = calls = planner = tokens = 0
    real_calls = 0
    latency = 0.0
    for path in paths:
        if not is_completed(path):
            continue
        recs = read_trace(path)
        if match and any(recs[0].get(k) != v for k, v in match.items()):
            continue
        steps = [r for r in recs if r.get("type") == "step"]
        runs += 1
        calls += len(steps)
        planner += sum(s["agent"] == "planner" for s in steps)
        for s in steps:
            u = s["usage"]
            tokens += max(u["prompt_tokens"] + u["completion_tokens"], u.get("provider_total_tokens") or 0)
            if not s.get("cached"):
                real_calls += 1
                latency += s["latency_s"]
    if runs == 0 or real_calls == 0:
        return None
    return Measured(source, runs, calls / runs, planner / runs, tokens / calls, latency / real_calls)


def estimate(
    specs: list[RunSpec],
    traces_dir: Path,
    smoke_dir: Path,
    pcfg: ProviderConfig,
    workers: int,
) -> dict:
    remaining = [s for s in specs if not is_completed(trace_path(traces_dir, s.run_id))]
    m = measure([trace_path(traces_dir, s.run_id) for s in specs], "трассы эксперимента")
    if m is None and smoke_dir.exists():
        smoke = sorted(smoke_dir.glob("*.jsonl"))
        m = measure(smoke, f"smoke-трассы ({smoke_dir.name})",
                    {"model": pcfg.model, "reasoning_effort": pcfg.reasoning_effort})
        if m is None:
            m = measure(smoke, f"smoke-трассы ({smoke_dir.name}) с ДРУГИМ reasoning_effort", {"model": pcfg.model})
    if m is None:
        m = Measured("априорные значения (реальных трасс нет)", 0, **PRIOR)

    n = len(remaining)
    calls = n * m.calls_per_run
    # Planner без changelog получает дословно одинаковый промпт во всех условиях одной задачи и
    # повтора — кэш отдаёт его один раз на блок. Остальные вызовы считаем различными.
    planner_keys = {(s.task.task_id, s.repeat_idx, s.condition.changelog_for_agent("planner")) for s in remaining}
    requests = n * (m.calls_per_run - m.planner_calls_per_run) + len(planner_keys) * m.planner_calls_per_run
    if not pcfg.cache:
        requests = calls
    tokens = requests * m.tokens_per_call
    run_s = m.calls_per_run * m.latency_per_call_s

    limits = {"задержка": max(1, workers) / run_s if run_s else float("inf")}  # прогонов в секунду
    if pcfg.rpm and requests:
        limits["RPM"] = pcfg.rpm / 60 / (requests / n)
    if pcfg.tpm and tokens:
        limits["TPM"] = pcfg.tpm / 60 / (tokens / n)
    binding = min(limits, key=limits.get) if n else "-"
    wall_s = n / limits[binding] if n else 0.0

    days = {}
    if pcfg.rpd:
        days["по RPD"] = requests / pcfg.rpd
    if pcfg.tpd:
        days["по TPD"] = tokens / pcfg.tpd
    return {
        "runs_in_grid": len(specs),
        "runs_completed": len(specs) - n,
        "runs_remaining": n,
        "source": f"{m.source}, прогонов: {m.runs}",
        "llm_calls_per_run": round(m.calls_per_run, 2),
        "latency_per_call_s": round(m.latency_per_call_s, 1),
        "llm_calls_remaining": round(calls),
        "api_requests_remaining": round(requests),
        "tokens_remaining": round(tokens),
        "workers": workers,
        "limits": {"rpm": pcfg.rpm, "tpm": pcfg.tpm, "rpd": pcfg.rpd or "неизвестен", "tpd": pcfg.tpd or "неизвестен"},
        "bottleneck": binding,
        "wall_hours": round(wall_s / 3600, 2),
        "days": {k: round(v, 2) for k, v in days.items()} or "дневные лимиты не заданы",
    }
