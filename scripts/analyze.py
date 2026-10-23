"""Анализ эксперимента (этап 2а): reports/analysis.md и reports/success_by_condition.png.

    python scripts/analyze.py --experiment main --provider gemini

Работает на частичном results.csv: n по каждому условию выводится явно.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from mas_drift.analysis import (  # noqa: E402
    SUBSETS, _version_key, all_paired, condition_order, load_results, ops_table, rows_from_traces,
    success_table, understood_not_done,
)
from mas_drift.infra.config import PROJECT_ROOT  # noqa: E402

# Категориальные слоты 1–2 эталонной палитры (проверены validate_palette: CVD ΔE 24.7, контраст ≥ 3:1).
COLORS = {"A0": "#2a78d6", "A1": "#eb6834"}
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"


def _pct(x: float) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def _ci(cell) -> str:
    n, ci = cell
    if ci is None:
        return "нет данных"
    m, lo, hi = ci
    return f"{_pct(m)} [{_pct(lo)}–{_pct(hi)}]"


def _p(p: float) -> str:
    return "—" if math.isnan(p) else ("<0.001" if p < 0.001 else f"{p:.3f}")


def _table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def plot(succ: list[dict], versions: list[str], path: Path) -> None:
    fig, axes = plt.subplots(1, len(SUBSETS), figsize=(12, 4.6), sharey=True, facecolor=SURFACE)
    width = 0.38
    for ax, subset in zip(axes, SUBSETS):
        ax.set_facecolor(SURFACE)
        by_cond = {r["condition"]: r[subset] for r in succ}
        for j, aw in enumerate(("A0", "A1")):
            for i, v in enumerate(versions):
                n, ci = by_cond.get(f"{v}:{aw}", (0, None))
                x = i + (j - 0.5) * (width + 0.02)  # зазор между соседними столбцами
                if ci is None:
                    ax.text(x, 0.02, "нет\nданных", ha="center", va="bottom", fontsize=7, color=INK_2)
                    continue
                m, lo, hi = ci
                ax.bar(x, m, width, color=COLORS[aw], label=aw if i == 0 else None, zorder=2)
                ax.errorbar(x, m, yerr=[[m - lo], [hi - m]], fmt="none", ecolor=INK_2, elinewidth=1.2,
                            capsize=3, zorder=3)
                ax.text(x, min(hi, 1.0) + 0.025, f"n={n}", ha="center", va="bottom", fontsize=7, color=INK_2)
        ax.set_xticks(range(len(versions)), versions, fontsize=9, color=INK)
        ax.set_ylim(0, 1.12)
        ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"], color=INK_2, fontsize=9)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(INK_2)
        ax.tick_params(length=0)
        ax.set_title({"все задачи": "Все задачи", "с решением": "Задачи с решением (gold_kind = pairs)"}[subset],
                     fontsize=11, color=INK, loc="left")
    axes[0].set_ylabel("Успешность (95% бутстрап-ДИ)", color=INK_2, fontsize=9)
    axes[-1].legend(title="Осведомлённость", frameon=False, fontsize=9, title_fontsize=9, loc="upper right",
                    bbox_to_anchor=(1.0, 1.12), ncols=2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def render(rows, exp_dir: Path, args, plot_name: str, source: str) -> str:
    versions = sorted({r["api_version"] for r in rows}, key=_version_key)
    conds = condition_order(rows, versions)
    succ = success_table(rows, conds, args.n_boot, args.seed)
    pairs = all_paired(rows, versions)
    ops = ops_table(rows, conds)
    gap = understood_not_done(rows, exp_dir / "traces")

    blocks = {(r["task_id"], r["repeat_idx"]) for r in rows}
    per_cond = {c: sum(r["condition"] == c for r in rows) for c in conds}
    complete = sum(1 for b in blocks if sum((r["task_id"], r["repeat_idx"]) == b for r in rows) == len(conds))
    partial = any(n < len(blocks) for n in per_cond.values())

    out = [f"# Анализ эксперимента `{args.experiment}` ({args.provider})", "",
           f"Сгенерировано {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC; источник: {source}.",
           f"Прогонов: **{len(rows)}**; задач (task × repeat): {len(blocks)}; "
           f"полных блоков (задача во всех {len(conds)} условиях): {complete}.", ""]
    if partial:
        out += ["> ⚠ **Данные частичные.** Число прогонов по условиям различается; парные сравнения "
                "используют только задачи, пройденные в обоих условиях.", ""]
    out += ["n по условиям: " + ", ".join(f"`{c}` = {n}" for c, n in per_cond.items()), ""]

    out += ["## 1. Успешность по условиям", "",
            f"Бутстрап 95% ДИ: {args.n_boot} ресемплов по задачам, seed {args.seed}. "
            "«С решением» — только `gold_kind = pairs`: на задачах без решения наивный агент при v2_units "
            "«угадывает» no_option.", ""]
    out.append(_table(["Условие", "n (все)", "Успех (все)", "n (с решением)", "Успех (с решением)"],
                      [[f"`{r['condition']}`", r["все задачи"][0], _ci(r["все задачи"]),
                        r["с решением"][0], _ci(r["с решением"])] for r in succ]))
    out += ["", f"![Успешность по условиям]({plot_name})", ""]

    out += ["## 2. Парные сравнения по задачам", "",
            "Точный тест Макнемара (биномиальный по несогласованным парам). ✓→✗ — задача, пройденная в "
            "базовом условии и проваленная в сравниваемом. Поправка Холма — внутри каждого семейства "
            "сравнений и подмножества задач.", ""]
    for subset in SUBSETS:
        for family in dict.fromkeys(p.family for p in pairs):
            grp = [p for p in pairs if p.subset == subset and p.family == family]
            out += [f"### {family} — {subset}", ""]
            out.append(_table(
                ["База → сравнение", "n пар", "оба ✓", "✓→✗", "✗→✓", "оба ✗", "Успех база → сравн.", "p", "p (Холм)"],
                [[f"`{p.base}` → `{p.other}`", p.n_pairs, p.both_pass, p.pass_to_fail, p.fail_to_pass,
                  p.both_fail, f"{_pct(p.base_rate)} → {_pct(p.other_rate)}", _p(p.p), _p(p.p_holm)]
                 for p in grp]))
            out.append("")

    out += ["## 3. Провалы и операционные метрики", "",
            "Доля ошибок инструментов = ошибочные результаты / все вызовы инструментов условия.", ""]
    out.append(_table(
        ["Условие", "n", "Причины провалов", "Доля ошибок инстр.", "Прогонов с ошибкой", "Лимит шагов",
         "LLM-вызовов/прогон", "Токенов/прогон", "cache_hits/прогон", "json_fenced"],
        [[f"`{o['condition']}`", o["n"],
          ", ".join(f"{k}: {v}" for k, v in sorted(o["fail_reasons"].items())) or "—",
          _pct(o["tool_error_share"]), o["runs_with_tool_error"], o["step_limit"],
          f"{o['mean_llm_calls']:.1f}" if o["n"] else "—", f"{o['mean_tokens']:.0f}" if o["n"] else "—",
          f"{o['mean_cache_hits']:.1f}" if o["n"] else "—", o["json_fenced"]] for o in ops]))
    out.append("")

    c = gap["counts"]
    out += [f"## 4. «Понял, но не сделал»: провалы `{gap['condition']}`", "",
            "Эвристика по тексту финального отчёта Executor (без LLM): упоминание `num_nights` или "
            "несовпадения схемы при том, что ни одного вызова с `num_nights` не было.", ""]
    out.append(_table(["Показатель", "Прогонов"], [
        ["Провалов условия", gap["failures"]],
        ["Отчёт упоминает `num_nights`", c["упоминает num_nights"]],
        ["Отчёт упоминает схему / несовпадение", c["упоминает схему/несовпадение"]],
        ["Был вызов с `num_nights`", c["вызывал num_nights"]],
        ["**Понял, но не сделал**", c["понял, но не сделал"]],
        ["Нет отчёта (лимит шагов)", c["нет отчёта (лимит шагов)"]],
    ]))
    if gap["examples"]:
        out += ["", "Примеры отчётов:", ""] + [f"- {e}" for e in gap["examples"]]
    out.append("")
    return "\n".join(out), succ, versions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", default="main")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--runs-dir", type=Path, default=PROJECT_ROOT / "runs")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports")
    ap.add_argument("--n-boot", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--csv", action="store_true",
                    help="читать results.csv вместо трасс (раннер пишет его только в конце или при остановке)")
    args = ap.parse_args(argv)

    exp_dir = args.runs_dir / args.experiment / args.provider
    if args.csv:
        path = exp_dir / "results.csv"
        if not path.exists():
            print(f"Нет {path}")
            return 1
        rows, source = load_results(path), f"`{path}`"
    else:
        rows, dupes = rows_from_traces(exp_dir / "traces")
        source = f"завершённые трассы `{exp_dir / 'traces'}`"
        if dupes:
            source += f" (отброшено дублей: {dupes})"
    if not rows:
        print(f"Нет завершённых прогонов в {exp_dir}")
        return 1
    plot_name = "success_by_condition.png"
    md, succ, versions = render(rows, exp_dir, args, plot_name, source)
    args.out.mkdir(parents=True, exist_ok=True)
    plot(succ, versions, args.out / plot_name)
    (args.out / "analysis.md").write_text(md, encoding="utf-8")
    print(f"{args.out / 'analysis.md'}\n{args.out / plot_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
