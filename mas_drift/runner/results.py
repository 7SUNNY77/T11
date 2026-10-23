"""results.csv (одна строка = один завершённый прогон) и сводка по условиям. Всё строится из трасс."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from mas_drift.agents.prompts import CHANGELOG_HEADER
from mas_drift.infra.trace import is_completed, read_trace, trace_path
from mas_drift.runner.grid import RunSpec
from mas_drift.tools.changelogs import IRRELEVANT_CHANGELOG, changelog_for

COLUMNS = [
    "run_id", "experiment", "provider", "model", "models_reported", "reasoning_effort", "temperature",
    "prompt_version", "api_version", "awareness", "changelog_to", "task_id", "repeat_idx", "block", "order",
    "gold_kind", "designed_as", "success", "reason", "answer_kind", "pair_ok", "total_ok", "json_fenced",
    "executor_status", "executor_steps", "llm_calls", "api_requests", "cache_hits",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
    "llm_latency_s", "wait_s", "duration_s", "tool_calls", "tool_errors", "drift_affected_results",
    "changelog_planner", "changelog_executor", "changelog_verifier", "started_at", "ended_at",
]

AGENTS = ("planner", "executor", "verifier")


def changelog_seen(records: list[dict], agent: str, api_version: str) -> str:
    """Какой changelog фактически был в системном промпте агента: none / own / irrelevant / other."""
    for r in records:
        if r.get("type") == "step" and r.get("agent") == agent:
            system = r["messages"][0]["content"]
            if CHANGELOG_HEADER not in system:
                return "none"
            if IRRELEVANT_CHANGELOG in system:
                return "irrelevant"
            if api_version != "v1" and changelog_for(api_version) in system:  # для stale — changelog v2_contract
                return "own"
            return "other"
    return "absent"


def row_from_trace(records: list[dict]) -> dict:
    start, end = records[0], records[-1]
    cond, chk, st = start["condition"], end["check"], end["stats"]
    row = {
        "run_id": start["run_id"], "experiment": start["experiment"], "provider": start["provider"],
        "model": start["model"], "models_reported": ";".join(st["models_reported"]),
        "reasoning_effort": start["reasoning_effort"], "temperature": start["temperature"],
        "prompt_version": start["prompt_version"], "api_version": cond["api_version"],
        "awareness": cond["awareness"], "changelog_to": ";".join(cond["changelog_to"]),
        "task_id": start["task_id"], "repeat_idx": start["repeat_idx"], "block": start["block"],
        "order": start["order"], "gold_kind": start["gold_kind"], "designed_as": start["designed_as"],
        "success": chk["success"], "reason": chk["reason"] or "", "answer_kind": chk["answer_kind"] or "",
        "pair_ok": chk["pair_ok"], "total_ok": chk["total_ok"], "json_fenced": chk["json_fenced"],
        "executor_status": end["executor_status"], "executor_steps": end["executor_steps"],
        "duration_s": end["duration_s"], "started_at": start["started_at"], "ended_at": end["ended_at"],
    }
    for k in ("llm_calls", "api_requests", "prompt_tokens", "completion_tokens",
              "reasoning_tokens", "total_tokens", "tool_calls", "tool_errors", "drift_affected_results"):
        row[k] = st[k]
    row["cache_hits"] = st["cached_calls"]  # LLM-вызовы этого прогона, ответ на которые взят из кэша
    row["llm_latency_s"] = round(st["llm_latency_s"], 3)
    row["wait_s"] = round(st["wait_s"], 3)
    for agent in AGENTS:
        row[f"changelog_{agent}"] = changelog_seen(records, agent, cond["api_version"])
    return row


def collect_rows(specs: list[RunSpec], traces_dir: Path) -> list[dict]:
    rows = []
    for spec in specs:
        path = trace_path(traces_dir, spec.run_id)
        if is_completed(path):
            rows.append(row_from_trace(read_trace(path)))
    return rows


def write_csv(rows: list[dict], path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _rate(rows: list[dict]) -> str:
    return f"{sum(r['success'] for r in rows)}/{len(rows)}" if rows else "-"


def summarize(rows: list[dict], condition_ids: list[str]) -> list[dict]:
    by_cond: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cond[f"{r['api_version']}:{r['awareness']}"].append(r)
    out = []
    for cid in condition_ids:
        rs = by_cond.get(cid, [])
        n = len(rs) or 1
        reasons = Counter(r["reason"] for r in rs if not r["success"])
        out.append({
            "condition": cid,
            "runs": len(rs),
            "success": _rate(rs),
            "success_solvable": _rate([r for r in rs if r["gold_kind"] == "pairs"]),
            "success_no_option": _rate([r for r in rs if r["gold_kind"] == "no_option"]),
            "fail_reasons": ";".join(f"{k}={v}" for k, v in sorted(reasons.items())),
            "tool_errors": sum(r["tool_errors"] for r in rs),
            "drift_affected": sum(r["drift_affected_results"] for r in rs),
            "step_limit": sum(r["executor_status"] == "step_limit" for r in rs),
            "mean_llm_calls": round(sum(r["llm_calls"] for r in rs) / n, 2),
            "mean_tokens": round(sum(r["total_tokens"] for r in rs) / n),
            "mean_latency_s": round(sum(r["llm_latency_s"] for r in rs) / n, 1),
        })
    return out


def complete_blocks(rows: list[dict], n_conditions: int) -> int:
    """Сколько пар (задача, повтор) пройдено во всех условиях — база для парного анализа."""
    per_block = Counter((r["task_id"], r["repeat_idx"]) for r in rows)
    return sum(1 for v in per_block.values() if v == n_conditions)


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "(нет данных)"
    cols = list(rows[0])
    widths = {c: max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols}
    line = lambda r: "  ".join(str(r[c]).ljust(widths[c]) for c in cols)  # noqa: E731
    return "\n".join([line({c: c for c in cols}), *(line(r) for r in rows)])
