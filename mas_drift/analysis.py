"""Анализ results.csv (этап 2а). Работает и на частичных данных: n по каждому условию всегда в отчёте.

Единица ресемплинга и парного сравнения — задача (task_id × repeat_idx): условия одной задачи
коррелированы (общая задача и общий кэш), поэтому независимы только задачи.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BOOL_COLS = ("success", "pair_ok", "total_ok", "json_fenced")
INT_COLS = ("repeat_idx", "llm_calls", "api_requests", "cache_hits", "total_tokens", "tool_calls",
            "tool_errors", "drift_affected_results", "executor_steps")
SUBSETS = {"все задачи": None, "с решением": "pairs"}


def load_results(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for c in BOOL_COLS:
            r[c] = r[c] == "True"
        for c in INT_COLS:
            r[c] = int(r[c])
        r["condition"] = f"{r['api_version']}:{r['awareness']}"
    return rows


def rows_from_traces(traces_dir: Path) -> tuple[list[dict], int]:
    """Строки results.csv из завершённых трасс — работает, пока раннер ещё идёт (results.csv он
    пишет только в конце). Дубликаты (условие, задача, повтор) — например, от прогонов со старой
    схемой run_id — схлопываются в самый поздний; возвращается и число отброшенных дублей."""
    from mas_drift.infra.trace import is_completed, read_trace
    from mas_drift.runner.results import row_from_trace

    latest: dict[tuple, dict] = {}
    dupes = 0
    for path in sorted(Path(traces_dir).glob("*.jsonl")):
        if not is_completed(path):
            continue
        r = row_from_trace(read_trace(path))
        r["condition"] = f"{r['api_version']}:{r['awareness']}"
        key = (r["condition"], r["task_id"], r["repeat_idx"])
        if key in latest:
            dupes += 1
            if r["started_at"] < latest[key]["started_at"]:
                continue
        latest[key] = r
    return list(latest.values()), dupes


def complete_blocks_only(rows: list[dict], conditions: list[str]) -> tuple[list[dict], set, set]:
    """Оставляет прогоны только тех блоков (задача × повтор), что пройдены во ВСЕХ условиях сетки.
    Возвращает (строки, полные блоки, все блоки)."""
    have: dict[tuple, set] = defaultdict(set)
    for r in rows:
        have[(r["task_id"], r["repeat_idx"])].add(r["condition"])
    need = set(conditions)
    complete = {b for b, cs in have.items() if need <= cs}
    kept = [r for r in rows if (r["task_id"], r["repeat_idx"]) in complete and r["condition"] in need]
    return kept, complete, set(have)


def _subset(rows: list[dict], gold_kind: str | None) -> list[dict]:
    return [r for r in rows if gold_kind is None or r["gold_kind"] == gold_kind]


def task_outcomes(rows: list[dict], condition: str, gold_kind: str | None = None) -> dict[tuple, bool]:
    return {(r["task_id"], r["repeat_idx"]): r["success"]
            for r in _subset(rows, gold_kind) if r["condition"] == condition}


# --- статистика ---

def bootstrap_ci(values: list[float], n_boot: int = 10_000, seed: int = 20261023,
                 level: float = 0.95) -> tuple[float, float, float] | None:
    """Перцентильный бутстрап среднего по задачам: (среднее, нижняя, верхняя граница)."""
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, len(arr), size=(n_boot, len(arr)))].mean(axis=1)
    alpha = (1 - level) / 2
    return float(arr.mean()), float(np.quantile(means, alpha)), float(np.quantile(means, 1 - alpha))


def mcnemar_exact(b: int, c: int) -> float:
    """Точный двусторонний тест Макнемара: биномиальный тест по несогласованным парам."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(pvalues: list[float]) -> list[float]:
    """Поправка Холма–Бонферрони (step-down), монотонная, обрезанная до 1."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvalues[i])
        adjusted[i] = min(1.0, running)
    return adjusted


@dataclass
class Paired:
    family: str
    subset: str
    base: str
    other: str
    n_pairs: int
    both_pass: int
    pass_to_fail: int  # base успех → other провал
    fail_to_pass: int  # base провал → other успех
    both_fail: int
    p: float
    p_holm: float = float("nan")

    @property
    def base_rate(self) -> float:
        return (self.both_pass + self.pass_to_fail) / self.n_pairs if self.n_pairs else float("nan")

    @property
    def other_rate(self) -> float:
        return (self.both_pass + self.fail_to_pass) / self.n_pairs if self.n_pairs else float("nan")


def paired(rows: list[dict], base: str, other: str, gold_kind: str | None, family: str, subset: str) -> Paired:
    a, b = task_outcomes(rows, base, gold_kind), task_outcomes(rows, other, gold_kind)
    keys = sorted(set(a) & set(b))
    t = Counter((a[k], b[k]) for k in keys)
    return Paired(family, subset, base, other, len(keys), t[(True, True)], t[(True, False)],
                  t[(False, True)], t[(False, False)], mcnemar_exact(t[(True, False)], t[(False, True)]))


def comparison_families(versions: list[str]) -> dict[str, list[tuple[str, str]]]:
    return {
        "а) эффект изменения API (v2_*:A0 против v1:A0)":
            [("v1:A0", f"{v}:A0") for v in versions if v != "v1"],
        "б) эффект changelog (A1 против A0 внутри версии)":
            [(f"{v}:A0", f"{v}:A1") for v in versions],
    }


def all_paired(rows: list[dict], versions: list[str]) -> list[Paired]:
    """Холм применяется внутри каждого семейства сравнений отдельно для каждого подмножества задач."""
    out = []
    for subset, gold_kind in SUBSETS.items():
        for family, pairs in comparison_families(versions).items():
            group = [paired(rows, b, o, gold_kind, family, subset) for b, o in pairs]
            for g, adj in zip(group, holm([g.p for g in group])):
                g.p_holm = adj
            out.extend(group)
    return out


# --- описательные таблицы ---

def condition_order(rows: list[dict], versions: list[str] | None = None) -> list[str]:
    versions = versions or sorted({r["api_version"] for r in rows}, key=_version_key)
    awareness = sorted({r["awareness"] for r in rows})
    return [f"{v}:{a}" for v in versions for a in awareness]


def _version_key(v: str) -> tuple:
    order = ["v1", "v2_contract", "v2_contract_stale", "v2_units", "v2_benign"]
    return (order.index(v) if v in order else len(order), v)


def success_table(rows: list[dict], conditions: list[str], n_boot: int, seed: int) -> list[dict]:
    out = []
    for cond in conditions:
        rec = {"condition": cond}
        for subset, gold_kind in SUBSETS.items():
            vals = [float(s) for s in task_outcomes(rows, cond, gold_kind).values()]
            ci = bootstrap_ci(vals, n_boot, seed)
            rec[subset] = (len(vals), ci)
        out.append(rec)
    return out


def ops_table(rows: list[dict], conditions: list[str]) -> list[dict]:
    out = []
    for cond in conditions:
        rs = [r for r in rows if r["condition"] == cond]
        n = len(rs)
        calls = sum(r["tool_calls"] for r in rs)
        out.append({
            "condition": cond,
            "n": n,
            "fail_reasons": Counter(r["reason"] for r in rs if not r["success"]),
            "tool_error_share": sum(r["tool_errors"] for r in rs) / calls if calls else float("nan"),
            "runs_with_tool_error": sum(r["tool_errors"] > 0 for r in rs),
            "step_limit": sum(r["executor_status"] == "step_limit" for r in rs),
            "mean_llm_calls": sum(r["llm_calls"] for r in rs) / n if n else float("nan"),
            "mean_tokens": sum(r["total_tokens"] for r in rs) / n if n else float("nan"),
            "mean_cache_hits": sum(r["cache_hits"] for r in rs) / n if n else float("nan"),
            "json_fenced": sum(r["json_fenced"] for r in rs),
        })
    return out


# --- «понял, но не сделал» ---

MENTION_PARAM = re.compile(r"num_nights", re.IGNORECASE)
MENTION_SCHEMA = re.compile(r"schema|mismatch|несоответств|несовпад|renamed|переименован", re.IGNORECASE)


def _executor_report(records: list[dict]) -> str | None:
    """Финальный отчёт Executor — его последний шаг без вызовов инструментов."""
    steps = [r for r in records if r.get("type") == "step" and r.get("agent") == "executor"]
    if steps and not steps[-1]["tool_calls"]:
        return steps[-1]["response"]["content"] or ""
    return None


def _called_param(records: list[dict], param: str) -> bool:
    for r in records:
        if r.get("type") == "step" and r.get("agent") == "executor":
            for tc in r["tool_calls"]:
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    continue
                if isinstance(args, dict) and param in args:
                    return True
    return False


def understood_not_done(rows: list[dict], traces_dir: Path, condition: str = "v2_contract_stale:A0") -> dict:
    """Провалы условия: упоминает ли отчёт Executor num_nights / несовпадение схемы, и был ли
    хоть один вызов с num_nights. Эвристика по тексту, без LLM."""
    from mas_drift.infra.trace import read_trace

    fails = [r for r in rows if r["condition"] == condition and not r["success"]]
    res = Counter()
    examples: list[str] = []
    for r in fails:
        path = Path(traces_dir) / f"{r['run_id']}.jsonl"
        if not path.exists():
            res["нет трассы"] += 1
            continue
        recs = read_trace(path)
        report = _executor_report(recs)
        called = _called_param(recs, "num_nights")
        if report is None:
            res["нет отчёта (лимит шагов)"] += 1
            continue
        param, schema = bool(MENTION_PARAM.search(report)), bool(MENTION_SCHEMA.search(report))
        res["упоминает num_nights"] += param
        res["упоминает схему/несовпадение"] += schema
        res["вызывал num_nights"] += called
        if (param or schema) and not called:
            res["понял, но не сделал"] += 1
            if len(examples) < 3:
                examples.append(f"{r['task_id']}: {report.replace(chr(10), ' ')[:240]}")
    return {"condition": condition, "failures": len(fails), "counts": res, "examples": examples}
