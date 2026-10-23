"""Smoke-прогон одной задачи на выбранном провайдере с подробным выводом шагов.

    python scripts/smoke.py --provider fake
    python scripts/smoke.py --provider gemini --task T01 --version v1 --awareness A0

Трасса пишется в runs/smoke/<provider>/ (отдельно от экспериментов). Кэш отключён:
smoke-тест должен видеть реальный ответ провайдера.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mas_drift.agents import Condition, run_mas  # noqa: E402
from mas_drift.domain import generate_tasks, generate_world  # noqa: E402
from mas_drift.eval import check  # noqa: E402
from mas_drift.infra.config import load_config  # noqa: E402
from mas_drift.infra.llm import build_llm  # noqa: E402
from mas_drift.infra.trace import TraceWriter, read_trace, trace_path  # noqa: E402


def _short(obj, n=300) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"... (+{len(s) - n})"


def _extra_keys(d, prefix="") -> list[str]:
    """Пути ключей без значений (подписи — длинные непрозрачные строки)."""
    out = []
    for k, v in d.items():
        out += _extra_keys(v, f"{prefix}{k}.") if isinstance(v, dict) else [f"{prefix}{k}"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="fake")
    ap.add_argument("--task", default="T01")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--awareness", default="A0")
    ap.add_argument("--repeat", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    exp = cfg.experiment
    pcfg = cfg.provider(args.provider)
    world = generate_world(exp.seed)
    task = {t.task_id: t for t in generate_tasks(world, exp.task_pool, exp.seed)}[args.task]
    cond = Condition(api_version=args.version, awareness=args.awareness,
                     changelog_to=cfg.awareness[args.awareness].changelog_to)
    llm = build_llm(pcfg, cache_dir=None)

    run_id = f"smoke_{cond.api_version}_{cond.awareness}_{task.task_id}_{time.strftime('%Y%m%d-%H%M%S')}"
    path = trace_path(exp.runs_dir / "smoke" / args.provider, run_id)
    print(f"provider={pcfg.name} model={pcfg.model} condition={cond.id} task={task.task_id}")
    print(f"request: {task.request}\ngold: {task.gold.model_dump()}\n")
    with TraceWriter(path, {"run_id": run_id, "condition": cond.model_dump(), "task_id": task.task_id,
                            "provider": pcfg.name, "model": pcfg.model}) as tw:
        res = run_mas(task, cond, world, llm, tw, args.repeat, exp.executor_max_steps)
        result = check(task, res.final_text)
        tw.finish(final_text=res.final_text, check=result.model_dump())

    for r in read_trace(path):
        if r["type"] != "step":
            continue
        print(f"[{r['step']}] {r['agent']}  finish={r['response']['finish_reason']}  "
              f"tokens={r['usage']}  latency={r['latency_s']}s  wait={r['wait_s']}s")
        if r["response"]["content"]:
            print("    content:", _short(r["response"]["content"]))
        for tc in r["tool_calls"]:
            extra = f"  extra keys: {_extra_keys(tc['extra'])}" if tc.get("extra") else ""
            print(f"    tool_call {tc['id']!r}: {tc['name']}({tc['arguments']}){extra}")
        if r.get("message_extra"):
            print(f"    message extra keys: {_extra_keys(r['message_extra'])}")
        for tr in r["tool_results"]:
            print(f"    result [{tr['drift_type']}]: {_short(tr['result'], 200)}")
    print(f"\nfinal: {res.final_text!r}")
    print(f"check: success={result.success} reason={result.reason} fenced={result.json_fenced}")
    print(f"executor_status={res.executor_status} stats={res.stats}")
    print(f"trace: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
