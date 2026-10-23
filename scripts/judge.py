"""LLM-судья атрибуции ошибок (этап 2б).

    python scripts/judge.py --trial          # по одному провалу v2_units:A0 и v2_contract_stale:A0, подробно
    python scripts/judge.py                  # все провалы основной сетки + reports/judge*.md
    python scripts/judge.py --report-only    # только пересобрать отчёты по уже сохранённым вердиктам
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mas_drift.infra.config import DEFAULT_CONFIG, PROJECT_ROOT, load_config  # noqa: E402
from mas_drift.infra.errors import InfraError  # noqa: E402
from mas_drift.infra.llm import build_llm  # noqa: E402
from mas_drift.judge.compress import TOKEN_BUDGET  # noqa: E402
from mas_drift.judge.run import (  # noqa: E402
    categories, failed_traces, judge_one, judge_setup, load_results, save, score, setup_hash, verdict_path,
)

TRIAL_CONDITIONS = ("v2_units_A0_", "v2_contract_stale_A0_")


def _frac(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "—"


def _table(header, rows) -> str:
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
                      *["| " + " | ".join(str(c) for c in r) + " |" for r in rows]])


def show(res: dict) -> None:
    u = res["usage"]
    print("=" * 100)
    print(f"{res['run_id']}  (условие {res['api_version']}:{res['awareness']}, причина провала {res['fail_reason']})")
    print(f"--- ВХОД СУДЬИ: {res['input_chars']} символов, оценка {res['input_est_tokens']} ток., "
          f"лимит текста {res['input_text_limit']}, утечки: {res['input_leaks'] or 'нет'}")
    print(res["input_text"])
    print(f"--- ОТВЕТ СУДЬИ (prompt_tokens={u['prompt_tokens']}, completion_tokens={u['completion_tokens']}, "
          f"reasoning_tokens={u['reasoning_tokens']}, total={u['provider_total_tokens']}, "
          f"latency={res['latency_s']} с, cached={res['cached']})")
    print(res["response"])
    print(f"--- разбор: {res['verdict'] or res['parse_error']}")
    print(f"--- истина: {res['truth']}")


def write_reports(results: list[dict], out: Path, setup: dict, shash: str, n_failures: int, seed: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sc = score(results)
    toks = [r["usage"]["prompt_tokens"] for r in results]
    leaked = [r for r in results if r["input_leaks"]]
    no_truth = [r for r in results if r["truth"]["status"] != "ok"]
    md = [
        "# LLM-судья: атрибуция ошибок", "",
        f"Судья: `{setup['model']}` ({setup['provider']}), temperature = {setup['temperature']}, "
        f"reasoning_effort = {setup['reasoning_effort']}, JSON-режим = {setup['json_mode']}, "
        f"промпт {setup['prompt_version']}, setup `{shash}`.", "",
        f"Провалов в завершённых трассах: {n_failures}; оценено: **{len(results)}**; "
        f"без истины (в метрики точности не входят): {len(no_truth)}.", "",
        f"Вход судьи (фактические prompt_tokens): среднее {sum(toks) / len(toks):.0f}, максимум {max(toks)}; "
        f"больше бюджета {TOKEN_BUDGET}: {sum(t > TOKEN_BUDGET for t in toks)}. "
        f"Найдено запрещённых строк во входе: {len(leaked)} прогонов"
        + (f" — {sorted({s[:40] for r in leaked for s in r['input_leaks']})}" if leaked else "") + ".", "",
        "**Истина**: виноват executor; истинный шаг — первый шаг executor, где он получил результат с "
        "drift_affected = true или ошибку инструмента. Точность по шагу засчитывается только при верном агенте.",
        "**Смещение вниз** — судья винит verifier, хотя истина — executor. **Причина названа** — эвристика по "
        "ключевым словам в root_cause (центы/×100 для v2_units; num_nights/схема/параметр для контрактных версий).",
        "",
        _table(["Условие", "n", "с истиной", "не разобран", "Агент", "Шаг точно", "Шаг ±1", "Смещение вниз",
                "Причина названа", "Ср. уверенность"],
               [[f"`{s.group}`", s.n, s.n_truth, s.parse_fail, _frac(s.agent_ok, s.n_truth),
                 _frac(s.step_exact, s.n_truth), _frac(s.step_pm1, s.n_truth), _frac(s.downstream, s.n_truth),
                 _frac(s.cause_ok, s.cause_n), f"{s.conf_sum / (s.n - s.parse_fail):.2f}" if s.n > s.parse_fail else "—"]
                for s in sc]),
        "", "## Категории ошибок по вердиктам", "",
    ]
    cats = categories(results)
    names = sorted({c for v in cats.values() for c in v})
    md.append(_table(["Условие", *names], [[f"`{k}`", *[v.get(c, 0) for c in names]] for k, v in sorted(cats.items())]))
    if no_truth:
        md += ["", "## Без истины", ""] + [f"- `{r['run_id']}`: {r['truth']['reason']}" for r in no_truth]
    (out / "judge.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with open(out / "judge_results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "api_version", "awareness", "task_id", "fail_reason", "truth_status", "truth_step",
                    "blamed_agent", "blamed_step", "error_category", "confidence", "root_cause", "prompt_tokens",
                    "completion_tokens", "parse_error"])
        for r in results:
            v, t = r["verdict"] or {}, r["truth"]
            w.writerow([r["run_id"], r["api_version"], r["awareness"], r["task_id"], r["fail_reason"], t["status"],
                        t.get("step"), v.get("blamed_agent"), v.get("blamed_step"), v.get("error_category"),
                        v.get("confidence"), v.get("root_cause"), r["usage"]["prompt_tokens"],
                        r["usage"]["completion_tokens"], r["parse_error"]])

    picks = random.Random(seed).sample(sorted(results, key=lambda r: r["run_id"]), min(5, len(results)))
    sm = [f"# Случайные вердикты судьи для ручной проверки (seed {seed})", ""]
    for i, r in enumerate(picks, 1):
        sm += [f"## {i}. `{r['run_id']}`", "",
               f"Условие (судья не видел): `{r['api_version']}:{r['awareness']}`; причина провала: {r['fail_reason']}; "
               f"истина: {r['truth']}", "",
               f"**Вердикт:** {r['verdict'] or r['parse_error']}", "",
               f"Вход судьи ({r['usage']['prompt_tokens']} prompt-токенов):", "", "```text", r["input_text"], "```", ""]
    (out / "judge_samples.md").write_text("\n".join(sm), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--experiment", default="main")
    ap.add_argument("--provider", default="gemini", help="провайдер агентов, чьи трассы судим")
    ap.add_argument("--judge", default="groq", help="провайдер судьи")
    ap.add_argument("--trial", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    pcfg = cfg.provider(args.judge)
    setup = judge_setup(pcfg)
    shash = setup_hash(setup)
    exp_dir = cfg.experiment.runs_dir / args.experiment / args.provider
    out_dir = exp_dir / "judge" / shash
    failures = failed_traces(exp_dir / "traces")
    print(f"Судья {pcfg.model} (temperature={pcfg.temperature}, reasoning_effort={pcfg.reasoning_effort}) | "
          f"провалов: {len(failures)} | вердикты: {out_dir}")

    if not args.report_only:
        if args.trial:
            todo = [next(p for p in failures if p.name.startswith(prefix)) for prefix in TRIAL_CONDITIONS]
        else:
            todo = [p for p in failures if not verdict_path(out_dir, p.stem).exists()]
            todo = todo[: args.limit] if args.limit else todo
        llm = build_llm(pcfg, cfg.experiment.cache_dir / "judge")
        for i, path in enumerate(todo, 1):
            try:
                res = judge_one(path, llm, setup)
            except InfraError as exc:
                print(f"Остановлено: {type(exc).__name__}: {str(exc)[:300]}. Запустите позже — оценённые пропустятся.")
                return 2
            save(res, out_dir)
            if args.trial:
                show(res)
            else:
                v = res["verdict"] or {}
                print(f"[{i}/{len(todo)}] {res['run_id']} → {v.get('blamed_agent')}@{v.get('blamed_step')} "
                      f"(истина {res['truth'].get('step')}) {v.get('error_category')} tokens={res['usage']['prompt_tokens']}")
        if args.trial:
            return 0

    results = load_results(out_dir)
    if not results:
        print("Нет вердиктов")
        return 1
    write_reports(results, args.out, setup, shash, len(failures), args.seed)
    print(f"{args.out / 'judge.md'}\n{args.out / 'judge_samples.md'}\n{args.out / 'judge_results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
