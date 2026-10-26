"""Финальный конвейер одной командой: анализ → судья на новых провалах → сводка и манифест.

    python scripts/finalize.py            # судья вызывается только для ещё не оценённых провалов
    python scripts/finalize.py --offline  # без API-вызовов: судья только пересобирает отчёты

Пишет в reports/: analysis.md, success_by_condition.png, judge.md, judge_samples.md, judge_results.csv,
results_all.csv, results_used.csv, summary.md, manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import analyze  # noqa: E402
import judge  # noqa: E402

from mas_drift.agents.prompts import PROMPT_VERSION  # noqa: E402
from mas_drift.analysis import (  # noqa: E402
    UNDERSTOOD_CONDITIONS, all_paired, complete_blocks_only, rows_from_traces, success_table, understood_not_done,
)
from mas_drift.infra.config import DEFAULT_CONFIG, load_config  # noqa: E402
from mas_drift.infra.trace import read_trace  # noqa: E402
from mas_drift.judge.compress import VIEWS  # noqa: E402
from mas_drift.judge.prompt import JUDGE_PROMPT_VERSION  # noqa: E402
from mas_drift.judge.run import judge_setup, load_results, score, self_report_crosstab, setup_hash  # noqa: E402
from mas_drift.runner.grid import version_fingerprint  # noqa: E402
from mas_drift.runner.results import COLUMNS, write_csv  # noqa: E402

N_BOOT, SEED = 10_000, 20261023  # те же, что у analyze.py по умолчанию


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _rel(path: Path) -> str:
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _ci(cell) -> str:
    n, ci = cell
    return "нет данных" if ci is None else f"{_pct(ci[0])} [{_pct(ci[1])}–{_pct(ci[2])}], n={n}"


def _p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def _frac(k, n) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "—"


def _table(header, rows) -> str:
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
                      *["| " + " | ".join(str(c) for c in r) + " |" for r in rows]])


def _git() -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout

    # Формат porcelain: «XY путь» — ведущий пробел значим, поэтому вывод не strip'аем.
    dirty = [ln[3:] for ln in run("status", "--porcelain").splitlines() if ln.strip()]
    return {"commit": run("rev-parse", "HEAD").strip(), "branch": run("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "dirty_files": [f for f in dirty if not f.startswith("reports/")]}


def consumption(traces_dir: Path) -> dict:
    """Расход агентов по ВСЕМ трассам эксперимента, включая прерванные (.part): их запросы тоже потрачены."""
    calls = requests = tokens = tokens_billed = cached = 0
    latency = 0.0  # только реальные вызовы: у кэш-попаданий записана задержка исходного вызова
    starts, ends, dates = [], [], set()
    n_done = n_part = 0
    for path in sorted(traces_dir.glob("*.jsonl*")):
        recs = read_trace(path)
        n_part += path.name.endswith(".part")
        n_done += not path.name.endswith(".part")
        starts.append(recs[0]["started_at"])
        dates.add(recs[0]["started_at"][:10])
        for r in recs:
            if r.get("type") == "step":
                calls += 1
                requests += r["api_requests"]
                cached += r["cached"]
                u = r["usage"]
                t = max(u["prompt_tokens"] + u["completion_tokens"], u.get("provider_total_tokens") or 0)
                tokens += t
                if not r["cached"]:
                    tokens_billed += t
                    latency += r["latency_s"]
                ends.append(r["ts"])
    return {"runs_completed": n_done, "runs_aborted": n_part, "llm_calls": calls, "http_requests": requests,
            "cache_hits": cached, "total_tokens": tokens, "billed_tokens": tokens_billed,
            "llm_latency_h": round(latency / 3600, 2),
            "first": min(starts) if starts else None, "last": max(ends) if ends else None,
            "dates_utc": sorted(dates)}


def judge_consumption(results_by_view: dict) -> dict:
    out = {}
    for view, rs in results_by_view.items():
        fresh = [r for r in rs if not r["cached"]]
        out[view] = {"verdicts": len(rs), "calls_not_cached": len(fresh),
                     "prompt_tokens": sum(r["usage"]["prompt_tokens"] for r in rs),
                     "completion_tokens": sum(r["usage"]["completion_tokens"] for r in rs),
                     "latency_min": round(sum(r["latency_s"] for r in rs) / 60, 1),
                     "first": min((r["judged_at"] for r in rs), default=None),
                     "last": max((r["judged_at"] for r in rs), default=None)}
    return out


def build_summary(cfg, args, exp_dir: Path, judge_rc: int) -> tuple[str, dict]:
    exp = cfg.experiment
    grid = [f"{v}:{a}" for v in exp.api_versions for a in exp.awareness]
    all_rows, _ = rows_from_traces(exp_dir / "traces")
    rows, complete, started = complete_blocks_only(all_rows, grid)
    write_csv(all_rows, args.out / "results_all.csv", COLUMNS)
    write_csv(rows, args.out / "results_used.csv", COLUMNS)

    succ = {r["condition"]: r for r in success_table(rows, grid, N_BOOT, SEED)}
    pairs = all_paired(rows, exp.api_versions)
    gaps = {c: understood_not_done(rows, exp_dir / "traces", c) for c in UNDERSTOOD_CONDITIONS}
    jcfg = cfg.provider(args.judge)
    jsetups = {v: judge_setup(jcfg, v) for v in VIEWS}
    jres = {v: load_results(exp_dir / "judge" / setup_hash(s), exp_dir / "traces") for v, s in jsetups.items()}
    jres = {v: rs for v, rs in jres.items() if rs}
    agents = consumption(exp_dir / "traces")
    jcons = judge_consumption(jres)
    acfg = cfg.provider(args.provider)

    src_csv = "`reports/results_used.csv`"
    md = [f"# Сводка эксперимента `{args.experiment}`", "",
          f"Сгенерировано {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Агенты: `{acfg.model}` "
          f"(temperature {acfg.temperature}, reasoning_effort {acfg.reasoning_effort}); судья: `{jcfg.model}` "
          f"(temperature {jcfg.temperature}, reasoning_effort {jcfg.reasoning_effort}). Параметры и хэши — "
          "`reports/manifest.json`.", "",
          f"**Объём: {len(complete)} полных блоков** (задача во всех {len(grid)} условиях) из {len(started)} начатых "
          f"и {exp.task_pool} запланированных; в статистике {len(rows)} прогонов из {len(all_rows)} завершённых. "
          f"Источник: {src_csv} (только полные блоки; все завершённые — `reports/results_all.csv`).", ""]
    if len(complete) < exp.task_pool:
        md += [f"> ⚠ Сетка не завершена: {len(complete)} из {exp.task_pool} задач.", ""]

    md += ["## 1. Успешность: A0 → A1", "",
           f"Доля `success = True` по условию; 95% перцентильный бутстрап по задачам ({N_BOOT} ресемплов, seed {SEED}). "
           f"Источник: {src_csv}, столбцы `api_version`, `awareness`, `success`, `gold_kind`; та же таблица — "
           "`reports/analysis.md` §1.", ""]
    md.append(_table(
        ["Версия API", "Все задачи: A0", "Все задачи: A1", "С решением: A0", "С решением: A1"],
        [[f"`{v}`", _ci(succ[f"{v}:A0"]["все задачи"]), _ci(succ[f"{v}:A1"]["все задачи"]),
          _ci(succ[f"{v}:A0"]["с решением"]), _ci(succ[f"{v}:A1"]["с решением"])] for v in exp.api_versions]))
    md += ["", "![Успешность по условиям](success_by_condition.png)", ""]

    md += ["## 2. Парные сравнения по задачам", "",
           "Точный тест Макнемара; Холм — внутри семейства и подмножества задач. ✓→✗ / ✗→✓ — переходы задач "
           f"из базового условия в сравниваемое. Источник: {src_csv} (пары по `task_id`, `repeat_idx`); "
           "полные таблицы — `reports/analysis.md` §2.", ""]
    for subset in ("с решением", "все задачи"):
        md += [f"**Задачи: {subset}**", ""]
        md.append(_table(["Сравнение", "n пар", "✓→✗", "✗→✓", "Успех база → сравн.", "p", "p (Холм)"],
                         [[f"`{p.base}` → `{p.other}`", p.n_pairs, p.pass_to_fail, p.fail_to_pass,
                           f"{_pct(p.base_rate)} → {_pct(p.other_rate)}", _p(p.p), _p(p.p_holm)]
                          for p in pairs if p.subset == subset]))
        md.append("")

    md += ["## 3. «Понял, но не сделал»", "",
           "Эвристика по тексту финального отчёта Executor (`mas_drift/analysis.py`: `understood_not_done`); "
           "трассы — `runs/<exp>/<provider>/traces`, прогоны — по `run_id` из "
           f"{src_csv}; подробности и примеры — `reports/analysis.md` §4.", ""]
    rows3 = []
    for c, g in gaps.items():
        cnt = g["counts"]
        rows3.append([f"`{c}`", g["failures"], cnt["понял, но не сделал"], cnt["нет отчёта (лимит шагов)"],
                      _frac(cnt["понял, но не сделал"], g["failures"])])
    md.append(_table(["Условие", "Провалов", "Понял, но не сделал", "Нет отчёта (лимит шагов)", "Доля"], rows3))
    md.append("")

    md += ["## 4. LLM-судья", "",
           "Все провалы завершённых трасс (не только полные блоки). Источник: `reports/judge_results.csv` "
           "(столбцы `view`, `blamed_agent`, `blamed_step`, `agent_error_step`, `drift_entry_step`, `root_cause`); "
           "определения и разбивка по условиям — `reports/judge.md`.", ""]
    if judge_rc == 2:
        md += ["> ⚠ Судья остановлен лимитом Groq; часть новых провалов не оценена — запустите finalize ещё раз.", ""]
    elif judge_rc not in (0, 2):
        md += [f"> ⚠ Судья завершился с кодом {judge_rc} — см. вывод finalize.", ""]
    if jres:
        tot = {v: score(rs)[-1] for v, rs in jres.items()}
        metrics = [("Оценено / с истиной", lambda s: f"{s.n} / {s.n_truth}"),
                   ("Агент верно", lambda s: _frac(s.agent_ok, s.n_truth)),
                   ("Шаг ошибки агента: точно", lambda s: _frac(s.step_exact, s.n_truth)),
                   ("Шаг ошибки агента: ±1", lambda s: _frac(s.step_pm1, s.n_truth)),
                   ("Шаг входа дрейфа: точно", lambda s: _frac(s.entry_exact, s.n_truth)),
                   ("Винит verifier", lambda s: _frac(s.blamed_verifier, s.n_truth)),
                   ("Причина названа (эвристика)", lambda s: _frac(s.cause_ok, s.cause_n)),
                   ("Не разобран", lambda s: s.parse_fail)]
        md.append(_table(["Метрика", *[f"`{v}`" for v in tot]], [[n, *[f(s) for s in tot.values()]] for n, f in metrics]))
    else:
        md.append("Вердиктов нет.")
    md.append("")

    mo = jres.get("messages_only")
    if mo:
        ct = self_report_crosstab(mo)
        md += ["### Судья в `messages_only` и самодиагностика Executor", "",
               "Назвал ли судья причину (`cause_named`) в зависимости от того, назвал ли её сам Executor в "
               "финальном отчёте (`executor_self_reported`; нет отчёта — «не назвал»). Источник: "
               "`reports/judge_results.csv`, строки `view = messages_only`, столбцы `cause_named`, "
               "`executor_self_reported`; шаблоны — `ROOT_CAUSE_KEYWORDS` в `mas_drift/judge/run.py` и "
               "`MENTION_*` в `mas_drift/analysis.py`.", ""]
        rows4 = []
        for key in sorted(ct, key=lambda k: (k == "ВСЕ", k)):
            c = ct[key]
            yes, no = c[(True, True)] + c[(True, False)], c[(False, True)] + c[(False, False)]
            rows4.append([f"`{key}`", c[(True, True)], c[(True, False)], c[(False, True)], c[(False, False)],
                          _frac(c[(True, True)], yes), _frac(c[(False, True)], no)])
        md.append(_table(["Условие", "сам ✓ / судья ✓", "сам ✓ / судья ✗", "сам ✗ / судья ✓", "сам ✗ / судья ✗",
                          "Судья назвал, если Executor назвал", "Судья назвал, если Executor не назвал"], rows4))
        md.append("")

    md += ["## 5. Расход", "",
           "Агенты — все трассы эксперимента, включая прерванные (`.part`); smoke и пилоты не входят. Источник: "
           "шаги трасс `runs/<exp>/<provider>/traces/*` (поля `api_requests`, `usage`, `latency_s`, `cached`); "
           f"по прогонам — {src_csv}, столбцы `llm_calls`, `api_requests`, `cache_hits`, `total_tokens`, "
           "`llm_latency_s`. Судья — файлы вердиктов (`usage`, `latency_s`, `cached`).", ""]
    md.append(_table(["Показатель", "Значение"], [
        ["Прогонов завершено / прервано", f"{agents['runs_completed']} / {agents['runs_aborted']}"],
        ["LLM-вызовов агентов (из них из кэша)", f"{agents['llm_calls']} ({agents['cache_hits']})"],
        ["HTTP-запросов к провайдеру агентов (нижняя оценка¹)", agents["http_requests"]],
        ["Токенов агентов: потрачено (без кэша) / всего в трассах",
         f"{agents['billed_tokens']:,} / {agents['total_tokens']:,}".replace(",", " ")],
        ["Суммарная задержка реальных LLM-вызовов агентов", f"{agents['llm_latency_h']} ч"],
        ["Окно прогонов (UTC)", f"{agents['first']} → {agents['last']}; дни: {', '.join(agents['dates_utc'])}"],
        *[[f"Судья `{v}`: вердиктов / вызовов не из кэша", f"{c['verdicts']} / {c['calls_not_cached']}"]
          for v, c in jcons.items()],
        *[[f"Судья `{v}`: токенов prompt + completion", f"{c['prompt_tokens']} + {c['completion_tokens']}"]
          for v, c in jcons.items()],
    ]))
    md += ["", "¹ Запросы оборванных попыток не учтены: при перезапуске трасса `.part` заменяется завершённой, "
           "а её шаги, повторённые из кэша, записаны с `api_requests = 0`.", ""]
    facts = {"grid": grid, "complete_blocks": len(complete), "started_blocks": len(started),
             "rows_used": len(rows), "rows_all": len(all_rows), "agents": agents, "judge": jcons,
             "judge_setups": {v: setup_hash(s) for v, s in jsetups.items()}}
    return "\n".join(md), facts


def build_manifest(cfg, args, exp_dir: Path, facts: dict) -> dict:
    exp = cfg.experiment
    acfg, jcfg = cfg.provider(args.provider), cfg.provider(args.judge)
    world, tasks = exp_dir / "world.json", exp_dir / "tasks.json"
    meta = json.loads((exp_dir / "meta.json").read_text(encoding="utf-8")) if (exp_dir / "meta.json").exists() else {}
    models_reported = sorted({m for r in rows_from_traces(exp_dir / "traces")[0] for m in r["models_reported"].split(";") if m})
    pkgs = {p: version(p) for p in ("openai", "pydantic", "python-dotenv", "numpy", "matplotlib", "pytest")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment": args.experiment,
        "git": _git(),
        "config": {"path": _rel(args.config), "sha256": _sha(args.config)},
        "agents": {**acfg.model_dump(exclude={"api_key_env"}), "models_reported": models_reported},
        "judge": jcfg.model_dump(exclude={"api_key_env"}),
        "experiment_config": exp.model_dump(mode="json", exclude={"runs_dir", "cache_dir"}),
        "awareness": {k: v.changelog_to for k, v in cfg.awareness.items()},
        "prompts": {"agents": PROMPT_VERSION, "judge": JUDGE_PROMPT_VERSION},
        "api_version_fingerprints": {v: version_fingerprint(v) for v in exp.api_versions},
        "data": {"seed": exp.seed, "world_json": {"path": _rel(world), "sha256": _sha(world),
                                                  "fingerprint": meta.get("world_fp")},
                 "tasks_json": {"path": _rel(tasks), "sha256": _sha(tasks),
                                "fingerprint": meta.get("tasks_fp")}},
        "analysis": {"bootstrap_resamples": N_BOOT, "bootstrap_seed": SEED, "complete_blocks_only": True,
                     "complete_blocks": facts["complete_blocks"], "rows_used": facts["rows_used"]},
        "runs": {k: facts["agents"][k] for k in ("first", "last", "dates_utc", "runs_completed", "runs_aborted")},
        "judge_runs": {"setups": facts["judge_setups"], **{v: {k: c[k] for k in ("first", "last", "verdicts")}
                                                           for v, c in facts["judge"].items()}},
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "packages": pkgs},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--experiment", default="main")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--judge", default="groq")
    ap.add_argument("--offline", action="store_true", help="без API-вызовов: судья только пересобирает отчёты")
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    exp_dir = cfg.experiment.runs_dir / args.experiment / args.provider
    common = ["--config", str(args.config), "--experiment", args.experiment, "--provider", args.provider]

    print("== 1/3 analyze (полные блоки)")
    rc = analyze.main([*common, "--out", str(args.out)])
    if rc != 0:
        return rc

    print("== 2/3 judge" + (" (офлайн: только отчёты)" if args.offline else " (только новые провалы)"))
    jargs = [*common, "--judge", args.judge, "--view", "both", "--out", str(args.out)]
    judge_rc = judge.main([*jargs, "--report-only"] if args.offline else jargs)
    if judge_rc == 2:
        print("Судья остановлен лимитом — собираю отчёты по уже оценённым")
        judge.main([*jargs, "--report-only"])
    elif judge_rc == 1:
        print("Вердиктов судьи нет — сводка соберётся без раздела судьи")
        judge_rc = 0

    print("== 3/3 summary + manifest")
    md, facts = build_summary(cfg, args, exp_dir, judge_rc)
    (args.out / "summary.md").write_text(md + "\n", encoding="utf-8")
    manifest = build_manifest(cfg, args, exp_dir, facts)
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest["git"]["dirty_files"]:
        print(f"⚠ рабочее дерево не закоммичено: {manifest['git']['dirty_files']}")
    for name in ("summary.md", "manifest.json", "analysis.md", "success_by_condition.png", "judge.md"):
        print(args.out / name)
    return 0 if judge_rc in (0, 2) else judge_rc


if __name__ == "__main__":
    raise SystemExit(main())
