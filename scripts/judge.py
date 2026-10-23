"""LLM-судья атрибуции ошибок (этап 2б).

    python scripts/judge.py --trial --view messages_only   # по одному провалу v2_units:A0 и v2_contract_stale:A0
    python scripts/judge.py --view both                    # все провалы в обоих режимах + reports/judge*.md
    python scripts/judge.py --report-only                  # пересобрать отчёты по сохранённым вердиктам

Режимы наблюдаемости: full — с ответами инструментов; messages_only — без них (см. judge/compress.py).
Истина пересчитывается из трасс при каждой сборке отчёта.
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
from mas_drift.judge.compress import TOKEN_BUDGET, VIEWS  # noqa: E402
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
    print(f"[{res['view']}] {res['run_id']}  (условие {res['api_version']}:{res['awareness']}, "
          f"причина провала {res['fail_reason']})")
    print(f"--- ВХОД СУДЬИ: {res['input_chars']} символов, оценка {res['input_est_tokens']} ток., "
          f"лимит текста {res['input_text_limit']}, утечки: {res['input_leaks'] or 'нет'}")
    print(res["input_text"])
    print(f"--- ОТВЕТ СУДЬИ (prompt_tokens={u['prompt_tokens']}, completion_tokens={u['completion_tokens']}, "
          f"reasoning_tokens={u['reasoning_tokens']}, latency={res['latency_s']} с, cached={res['cached']})")
    print(res["response"])
    print(f"--- разбор: {res['verdict'] or res['parse_error']}")
    print(f"--- истина: {res['truth']}")


def _view_section(view: str, results: list[dict], setup: dict, shash: str) -> list[str]:
    sc = score(results)
    toks = [r["usage"]["prompt_tokens"] for r in results]
    leaked = [r for r in results if r["input_leaks"]]
    md = [f"## Режим `{view}`", "",
          f"setup `{shash}`; оценено {len(results)}; вход (prompt_tokens): среднее {sum(toks) / len(toks):.0f}, "
          f"максимум {max(toks)}, больше {TOKEN_BUDGET}: {sum(t > TOKEN_BUDGET for t in toks)}; "
          f"прогонов с запрещёнными строками во входе: {len(leaked)}"
          + (f" — {sorted({s[:40] for r in leaked for s in r['input_leaks']})}" if leaked else "") + ".", "",
          _table(["Условие", "n", "с истиной", "не разобран", "Агент", "Шаг ошибки: точно", "Шаг ошибки: ±1",
                  "Шаг входа дрейфа: точно", "Шаг входа: ±1", "Винит verifier", "Смещение вниз",
                  "Причина названа", "Ср. уверенность"],
                 [[f"`{s.group}`", s.n, s.n_truth, s.parse_fail, _frac(s.agent_ok, s.n_truth),
                   _frac(s.step_exact, s.n_truth), _frac(s.step_pm1, s.n_truth),
                   _frac(s.entry_exact, s.n_truth), _frac(s.entry_pm1, s.n_truth),
                   _frac(s.blamed_verifier, s.n_truth), _frac(s.downstream, s.n_truth),
                   _frac(s.cause_ok, s.cause_n),
                   f"{s.conf_sum / (s.n - s.parse_fail):.2f}" if s.n > s.parse_fail else "—"] for s in sc]),
          "", "Категории ошибок:", ""]
    cats = categories(results)
    names = sorted({c for v in cats.values() for c in v})
    md.append(_table(["Условие", *names], [[f"`{k}`", *[v.get(c, 0) for c in names]] for k, v in sorted(cats.items())]))
    return md + [""]


def write_reports(by_view: dict[str, list[dict]], out: Path, setups: dict, hashes: dict, n_failures: int,
                  seed: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    any_setup = next(iter(setups.values()))
    md = [
        "# LLM-судья: атрибуция ошибок", "",
        f"Судья: `{any_setup['model']}` ({any_setup['provider']}), temperature = {any_setup['temperature']}, "
        f"reasoning_effort = {any_setup['reasoning_effort']}, JSON-режим = {any_setup['json_mode']}, "
        f"промпт {any_setup['prompt_version']}. Провалов в завершённых трассах: {n_failures}.", "",
        "**Режимы.** `full` — судья видит ответы инструментов (включая ошибки); `messages_only` — только сообщения "
        "агентов и вызовы инструментов с аргументами, без ответов (ближе к тому, что видит Verifier).", "",
        "**Истина.** Виноват executor. *Шаг ошибки агента* (основная метрика) — первый шаг, где executor действовал "
        "на основе дрейфа: для отклонённого вызова — шаг этого вызова, для изменённого выхода — следующий шаг executor. "
        "*Шаг входа дрейфа* (вспомогательная) — шаг, где executor получил результат с дрейфом или ошибку. "
        "Точность по шагу засчитывается только при верном агенте. Неразобранный вердикт считается ошибкой.", "",
        "**Смещение вниз** — винит verifier, хотя истина — executor. **Причина названа** — эвристика по ключевым "
        "словам в root_cause (центы/×100 для v2_units; num_nights/схема/параметр для контрактных версий).", "",
        "## Режимы рядом (все условия)", "",
    ]
    totals = {v: score(rs)[-1] for v, rs in by_view.items()}
    metrics = [("n с истиной", lambda s: s.n_truth), ("Агент", lambda s: _frac(s.agent_ok, s.n_truth)),
               ("Шаг ошибки: точно", lambda s: _frac(s.step_exact, s.n_truth)),
               ("Шаг ошибки: ±1", lambda s: _frac(s.step_pm1, s.n_truth)),
               ("Шаг входа дрейфа: точно", lambda s: _frac(s.entry_exact, s.n_truth)),
               ("Винит verifier", lambda s: _frac(s.blamed_verifier, s.n_truth)),
               ("Причина названа", lambda s: _frac(s.cause_ok, s.cause_n)),
               ("Не разобран", lambda s: s.parse_fail)]
    md.append(_table(["Метрика", *[f"`{v}`" for v in totals]], [[name, *[f(s) for s in totals.values()]]
                                                                 for name, f in metrics]))
    md.append("")
    for view, results in by_view.items():
        md += _view_section(view, results, setups[view], hashes[view])
    first = next(iter(by_view.values()))
    no_truth = [r for r in first if r["truth"]["status"] != "ok"]
    if no_truth:
        md += ["## Без истины (в метрики точности не входят)", ""] + [
            f"- `{r['run_id']}`: {r['truth']['reason']}" for r in no_truth]
    (out / "judge.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with open(out / "judge_results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["view", "run_id", "api_version", "awareness", "task_id", "fail_reason", "truth_status",
                    "agent_error_step", "drift_entry_step", "blamed_agent", "blamed_step", "error_category",
                    "confidence", "root_cause", "prompt_tokens", "completion_tokens", "parse_error"])
        for view, results in by_view.items():
            for r in results:
                v, t = r["verdict"] or {}, r["truth"]
                w.writerow([view, r["run_id"], r["api_version"], r["awareness"], r["task_id"], r["fail_reason"],
                            t["status"], t.get("agent_error_step"), t.get("drift_entry_step"), v.get("blamed_agent"),
                            v.get("blamed_step"), v.get("error_category"), v.get("confidence"), v.get("root_cause"),
                            r["usage"]["prompt_tokens"], r["usage"]["completion_tokens"], r["parse_error"]])

    # Одни и те же 5 прогонов во всех режимах — чтобы сравнить вердикты на одном входе.
    common = sorted(set.intersection(*[{r["run_id"] for r in rs} for rs in by_view.values()]))
    picks = random.Random(seed).sample(common, min(5, len(common)))
    idx = {view: {r["run_id"]: r for r in rs} for view, rs in by_view.items()}
    sm = [f"# Случайные вердикты судьи для ручной проверки (seed {seed})", ""]
    for i, run_id in enumerate(picks, 1):
        first = next(iter(idx.values()))[run_id]
        sm += [f"## {i}. `{run_id}`", "",
               f"Условие (судья не видел): `{first['api_version']}:{first['awareness']}`; причина провала: "
               f"{first['fail_reason']}; истина: {first['truth']}", ""]
        for view in idx:
            r = idx[view][run_id]
            sm += [f"**Вердикт `{view}`:** {r['verdict'] or r['parse_error']}", ""]
        for view in idx:
            r = idx[view][run_id]
            sm += [f"<details><summary>Вход судьи `{view}` ({r['usage']['prompt_tokens']} prompt-токенов)</summary>",
                   "", "```text", r["input_text"], "```", "", "</details>", ""]
    (out / "judge_samples.md").write_text("\n".join(sm), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--experiment", default="main")
    ap.add_argument("--provider", default="gemini", help="провайдер агентов, чьи трассы судим")
    ap.add_argument("--judge", default="groq", help="провайдер судьи")
    ap.add_argument("--view", choices=[*VIEWS, "both"], default="both")
    ap.add_argument("--trial", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    pcfg = cfg.provider(args.judge)
    views = list(VIEWS) if args.view == "both" else [args.view]
    setups = {v: judge_setup(pcfg, v) for v in views}
    hashes = {v: setup_hash(s) for v, s in setups.items()}
    exp_dir = cfg.experiment.runs_dir / args.experiment / args.provider
    traces_dir = exp_dir / "traces"
    out_dirs = {v: exp_dir / "judge" / hashes[v] for v in views}
    failures = failed_traces(traces_dir)
    print(f"Судья {pcfg.model} (temperature={pcfg.temperature}, reasoning_effort={pcfg.reasoning_effort}) | "
          f"режимы {views} | провалов: {len(failures)}")

    if not args.report_only:
        llm = build_llm(pcfg, cfg.experiment.cache_dir / "judge")
        for view in views:
            if args.trial:
                todo = [next(p for p in failures if p.name.startswith(prefix)) for prefix in TRIAL_CONDITIONS]
            else:
                todo = [p for p in failures if not verdict_path(out_dirs[view], p.stem).exists()]
                todo = todo[: args.limit] if args.limit else todo
            for i, path in enumerate(todo, 1):
                try:
                    res = judge_one(path, llm, setups[view])
                except InfraError as exc:
                    print(f"Остановлено: {type(exc).__name__}: {str(exc)[:300]}. Запустите позже — оценённые пропустятся.")
                    return 2
                save(res, out_dirs[view])
                if args.trial:
                    show(res)
                else:
                    v = res["verdict"] or {}
                    print(f"[{view} {i}/{len(todo)}] {res['run_id']} → {v.get('blamed_agent')}@{v.get('blamed_step')} "
                          f"(истина {res['truth'].get('agent_error_step')}) {v.get('error_category')} "
                          f"tokens={res['usage']['prompt_tokens']} cached={res['cached']}")
        if args.trial:
            return 0

    by_view = {v: load_results(out_dirs[v], traces_dir) for v in views}
    by_view = {v: rs for v, rs in by_view.items() if rs}
    if not by_view:
        print("Нет вердиктов")
        return 1
    write_reports(by_view, args.out, {v: setups[v] for v in by_view}, {v: hashes[v] for v in by_view},
                  len(failures), args.seed)
    print(f"{args.out / 'judge.md'}\n{args.out / 'judge_samples.md'}\n{args.out / 'judge_results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
