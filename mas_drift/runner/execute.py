"""Выполнение одного прогона и параллельный запуск сетки."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from mas_drift.agents.pipeline import run_mas
from mas_drift.domain.world import World
from mas_drift.eval.checker import check
from mas_drift.infra.errors import InfraError
from mas_drift.infra.llm import LLMClient
from mas_drift.infra.trace import TraceWriter, is_completed, trace_path
from mas_drift.runner.grid import RunSpec, version_fingerprint
from mas_drift.tools.versions import SPECS


def execute_run(
    spec: RunSpec,
    world: World,
    llm: LLMClient,
    traces_dir: Path,
    experiment: str,
    setup: dict,
) -> dict:
    cond, task = spec.condition, spec.task
    header = {
        "run_id": spec.run_id,
        "experiment": experiment,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **setup,
        "api_fp": version_fingerprint(cond.api_version),
        "condition": cond.model_dump(),
        "task_id": task.task_id,
        "repeat_idx": spec.repeat_idx,
        "block": spec.block,
        "order": spec.order,
        "request": task.request,
        "gold": task.gold.model_dump(),
        "gold_kind": task.gold.kind,
        "designed_as": task.designed_as,
        "tool_schemas": SPECS[cond.api_version].schemas,
    }
    t0 = time.monotonic()
    with TraceWriter(trace_path(traces_dir, spec.run_id), header) as tw:
        res = run_mas(task, cond, world, llm, tw, spec.repeat_idx, setup["executor_max_steps"])
        result = check(task, res.final_text)
        tw.finish(
            final_text=res.final_text,
            check=result.model_dump(),
            executor_status=res.executor_status,
            executor_steps=res.executor_steps,
            stats=asdict(res.stats),
            duration_s=round(time.monotonic() - t0, 3),
        )
    return {"run_id": spec.run_id, "success": result.success, "reason": result.reason,
            "executor_status": res.executor_status, "llm_calls": res.stats.llm_calls}


@dataclass
class GridReport:
    total: int
    skipped: int = 0
    completed: int = 0
    stopped_by: BaseException | None = None
    failed_run: str | None = None
    rows: list[dict] = field(default_factory=list)


def run_grid(
    specs: list[RunSpec],
    world: World,
    llm: LLMClient,
    traces_dir: Path,
    experiment: str,
    setup: dict,
    workers: int,
    max_runs: int | None = None,
    log: Callable[[str], None] = print,
) -> GridReport:
    """Запускает незавершённые прогоны. При ошибке (инфраструктурной или любой другой) новые
    прогоны не стартуют, уже идущие доделываются; незавершённые останутся .part и будут
    перезапущены при следующем вызове."""
    pending = [s for s in specs if not is_completed(trace_path(traces_dir, s.run_id))]
    report = GridReport(total=len(specs), skipped=len(specs) - len(pending))
    if max_runs is not None:
        pending = pending[:max_runs]
    queue = iter(pending)
    inflight: dict[Future, RunSpec] = {}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        def submit_next() -> None:
            spec = next(queue, None)
            if spec is not None:
                inflight[pool.submit(execute_run, spec, world, llm, traces_dir, experiment, setup)] = spec

        for _ in range(max(1, workers)):
            submit_next()
        try:
            while inflight:
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    spec = inflight.pop(fut)
                    try:
                        row = fut.result()
                    except BaseException as exc:  # noqa: BLE001 — останавливаем сетку, решает вызывающий
                        if report.stopped_by is None:
                            report.stopped_by, report.failed_run = exc, spec.run_id
                        kind = "инфраструктура" if isinstance(exc, InfraError) else "ОШИБКА"
                        log(f"[{kind}] {spec.run_id}: {type(exc).__name__}: {str(exc)[:300]}")
                        continue
                    report.completed += 1
                    report.rows.append(row)
                    log(f"[{report.skipped + report.completed}/{report.total}] {spec.run_id} "
                        f"success={row['success']} reason={row['reason']} calls={row['llm_calls']}")
                    if report.stopped_by is None:
                        submit_next()
        except KeyboardInterrupt as exc:
            report.stopped_by = exc
            log("Прервано: жду завершения идущих прогонов...")
            for fut in list(inflight):
                fut.cancel()
            raise
    return report
