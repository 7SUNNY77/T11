"""CLI раннера: python run.py --provider fake --conditions all --n-tasks 30 --repeats 1"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mas_drift.domain import generate_tasks, generate_world
from mas_drift.domain.tasks import tasks_fingerprint
from mas_drift.infra.config import DEFAULT_CONFIG, load_config
from mas_drift.infra.errors import InfraError
from mas_drift.infra.llm import LLMClient, build_llm
from mas_drift.runner.estimate import estimate
from mas_drift.runner.execute import run_grid
from mas_drift.runner.grid import build_grid, conditions_from_config, experiment_dir, run_setup
from mas_drift.runner.results import COLUMNS, collect_rows, complete_blocks, format_table, summarize, write_csv

EXIT_OK, EXIT_ERROR, EXIT_RESUME_LATER = 0, 1, 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Сетка эксперимента: версии API × осведомлённость")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--provider", default="fake")
    ap.add_argument("--conditions", default="all", help='"all" или список, например "v1:A0,v2_units:A1"')
    ap.add_argument("--n-tasks", type=int, default=None, help="префикс пула задач (по умолчанию весь пул)")
    ap.add_argument("--tasks", default=None, help="конкретные task_id через запятую, например T01,T05")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=None, help="по умолчанию из конфига")
    ap.add_argument("--experiment", default=None, help="имя эксперимента (папка в runs/)")
    ap.add_argument("--max-runs", type=int, default=None, help="выполнить не больше N новых прогонов")
    ap.add_argument("--estimate", action="store_true", help="только оценить бюджет, без вызовов")
    return ap.parse_args(argv)


def _check_meta(exp_dir: Path, meta: dict) -> None:
    """Защита возобновляемости: мир и задачи эксперимента не должны меняться между запусками."""
    path = exp_dir / "meta.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        diff = {k: (old.get(k), v) for k, v in meta.items() if old.get(k) != v}
        if diff:
            raise SystemExit(f"meta.json эксперимента не совпадает: {diff}. Используйте другое --experiment.")
        return
    exp_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None, llm: LLMClient | None = None, log=print) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    exp = cfg.experiment
    pcfg = cfg.provider(args.provider)
    workers = args.workers or exp.workers

    world = generate_world(exp.seed)
    pool = generate_tasks(world, exp.task_pool, exp.seed)
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",")]
        by_id = {t.task_id: t for t in pool}
        missing = [w for w in wanted if w not in by_id]
        if missing:
            raise SystemExit(f"Нет задач {missing} в пуле из {exp.task_pool}")
        tasks = [by_id[w] for w in wanted]
    else:
        tasks = pool[: args.n_tasks or exp.task_pool]

    exp_name = args.experiment or (exp.fake_name if pcfg.kind == "fake" else exp.name)
    try:
        exp_dir = experiment_dir(exp.runs_dir, exp_name, exp.fake_name, pcfg)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    traces_dir = exp_dir / "traces"
    world_fp, pool_fp = world.fingerprint(), tasks_fingerprint(pool)
    conditions = conditions_from_config(cfg, args.conditions)
    setup = run_setup(pcfg, exp.executor_max_steps, world_fp, pool_fp)
    specs = build_grid(conditions, tasks, args.repeats, setup, exp.order_seed)

    log(f"Эксперимент {exp_name} | провайдер {pcfg.name} ({pcfg.model}, reasoning_effort={pcfg.reasoning_effort}) | "
        f"{len(conditions)} условий × {len(tasks)} задач × {args.repeats} повт. = {len(specs)} прогонов | "
        f"воркеров {workers}")

    if args.estimate:
        est = estimate(specs, traces_dir, exp.runs_dir / "smoke" / pcfg.name, pcfg, workers)
        for k, v in est.items():
            log(f"  {k}: {v}")
        return EXIT_OK

    _check_meta(exp_dir, {"seed": exp.seed, "world_fp": world_fp, "tasks_fp": pool_fp, "provider_kind": pcfg.kind})
    (exp_dir / "world.json").write_text(world.model_dump_json(indent=1), encoding="utf-8")
    (exp_dir / "tasks.json").write_text(
        json.dumps([t.model_dump() for t in pool], ensure_ascii=False, indent=1), encoding="utf-8")

    if llm is None:
        llm = build_llm(pcfg, exp.cache_dir)
    report = run_grid(specs, world, llm, traces_dir, exp_name, setup, workers, args.max_runs, log=log)

    rows = collect_rows(specs, traces_dir)
    write_csv(rows, exp_dir / "results.csv", COLUMNS)
    summary = summarize(rows, [c.id for c in conditions])
    write_csv(summary, exp_dir / "summary.csv", list(summary[0]) if summary else ["condition"])
    log("\n" + format_table(summary))
    log(f"\nЗавершено прогонов: {len(rows)}/{len(specs)} (пропущено как готовые: {report.skipped}); "
        f"полных блоков задача×условия: {complete_blocks(rows, len(conditions))}/{len(specs) // max(1, len(conditions))}")
    log(f"results: {exp_dir / 'results.csv'}")

    if report.stopped_by is not None:
        if isinstance(report.stopped_by, InfraError):
            log(f"Остановлено: {type(report.stopped_by).__name__}. Запустите ту же команду позже — "
                "готовые прогоны будут пропущены.")
            return EXIT_RESUME_LATER
        log(f"Остановлено из-за ошибки в прогоне {report.failed_run}")
        raise report.stopped_by
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
