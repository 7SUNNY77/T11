import json
import sys
from pathlib import Path

import pytest

from mas_drift.analysis import (
    all_paired, bootstrap_ci, holm, mcnemar_exact, paired, rows_from_traces, understood_not_done,
)
from mas_drift.infra.trace import TraceWriter, trace_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_mcnemar_exact_known_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(2 / 32)
    assert mcnemar_exact(1, 9) == pytest.approx(2 * 11 / 1024)
    assert mcnemar_exact(9, 1) == mcnemar_exact(1, 9)
    assert mcnemar_exact(3, 3) == 1.0


def test_holm():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])
    assert holm([]) == []


def test_bootstrap_ci():
    assert bootstrap_ci([]) is None
    assert bootstrap_ci([1.0] * 10) == (1.0, 1.0, 1.0)
    vals = [1.0] * 15 + [0.0] * 15
    m, lo, hi = bootstrap_ci(vals, n_boot=5000, seed=1)
    assert m == 0.5 and 0.3 < lo < 0.5 < hi < 0.7
    assert bootstrap_ci(vals, n_boot=5000, seed=1) == (m, lo, hi)


def _row(cond, task, success, gold="pairs", rep=0):
    v, a = cond.split(":")
    return {"condition": cond, "api_version": v, "awareness": a, "task_id": task, "repeat_idx": rep,
            "success": success, "gold_kind": gold}


def test_paired_transitions_and_partial_pairs():
    rows = [_row("v1:A0", "T1", True), _row("v1:A0", "T2", True), _row("v1:A0", "T3", False),
            _row("v1:A0", "T4", False), _row("v1:A0", "T5", True),
            _row("v2_units:A0", "T1", True), _row("v2_units:A0", "T2", False), _row("v2_units:A0", "T3", True),
            _row("v2_units:A0", "T4", False)]  # T5 ещё не пройдена во втором условии
    p = paired(rows, "v1:A0", "v2_units:A0", None, "f", "s")
    assert (p.n_pairs, p.both_pass, p.pass_to_fail, p.fail_to_pass, p.both_fail) == (4, 1, 1, 1, 1)
    assert p.base_rate == 0.5 and p.other_rate == 0.5 and p.p == 1.0


def test_paired_subset_and_holm_families():
    rows = []
    for i in range(10):
        gold = "no_option" if i < 2 else "pairs"
        rows += [_row("v1:A0", f"T{i}", True, gold), _row("v1:A1", f"T{i}", True, gold),
                 _row("v2_units:A0", f"T{i}", gold == "no_option", gold), _row("v2_units:A1", f"T{i}", True, gold)]
    res = all_paired(rows, ["v1", "v2_units"])
    eff = [r for r in res if r.family.startswith("а)") and r.subset == "с решением"]
    assert len(eff) == 1 and eff[0].n_pairs == 8 and eff[0].pass_to_fail == 8
    assert eff[0].p == pytest.approx(2 / 2**8)
    cl = [r for r in res if r.family.startswith("б)") and r.subset == "все задачи"]
    assert [r.other for r in cl] == ["v1:A1", "v2_units:A1"]
    assert cl[1].p_holm == pytest.approx(min(1, 2 * cl[1].p)) and cl[0].p_holm == 1.0


def _trace(tmp_path, run_id, cond, calls, report, task="T01", started="2026-10-23T10:00:00"):
    header = {"run_id": run_id, "experiment": "x", "provider": "p", "model": "m", "reasoning_effort": "low",
              "temperature": 0.7, "prompt_version": "p1", "condition": cond, "task_id": task, "repeat_idx": 0,
              "block": 0, "order": 0, "gold_kind": "pairs", "designed_as": "plain", "started_at": started}
    stats = {"models_reported": ["m"], "llm_calls": len(calls) + 1, "api_requests": 1, "cached_calls": 0,
             "prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0, "total_tokens": 2, "tool_calls": 1,
             "tool_errors": 1, "drift_affected_results": 1, "llm_latency_s": 1.0, "wait_s": 0.0}
    with TraceWriter(trace_path(tmp_path, run_id), header) as tw:
        for args in calls:
            tw.step("executor", messages=[{"role": "system", "content": "s"}], response={"content": None},
                    tool_calls=[{"id": "c", "name": "search_hotels", "arguments": json.dumps(args)}], tool_results=[])
        tw.step("executor", messages=[{"role": "system", "content": "s"}],
                response={"content": report}, tool_calls=[], tool_results=[])
        tw.finish(final_text='{"no_option": true}', executor_status="ok", executor_steps=len(calls) + 1,
                  duration_s=1.0, stats=stats,
                  check={"success": False, "reason": "false_no_option", "answer_kind": "no_option",
                         "pair_ok": False, "total_ok": False, "json_fenced": False})


STALE = {"api_version": "v2_contract_stale", "awareness": "A0", "changelog_to": []}


def test_understood_not_done(tmp_path):
    _trace(tmp_path, "a", STALE, [{"city": "X", "nights": 2}], "Hotel search failed: API schema mismatch, needs 'num_nights'.", "T01")
    _trace(tmp_path, "b", STALE, [{"city": "X", "nights": 2}, {"city": "X", "num_nights": 2}], "num_nights also failed", "T02")
    _trace(tmp_path, "c", STALE, [{"city": "X", "nights": 2}], "No hotels found.", "T03")
    rows, dupes = rows_from_traces(tmp_path)
    assert dupes == 0 and len(rows) == 3
    gap = understood_not_done(rows, tmp_path)
    c = gap["counts"]
    assert gap["failures"] == 3
    assert c["упоминает num_nights"] == 2 and c["упоминает схему/несовпадение"] == 1
    assert c["вызывал num_nights"] == 1 and c["понял, но не сделал"] == 1
    assert gap["examples"][0].startswith("T01")


def test_rows_from_traces_dedupes_latest(tmp_path):
    _trace(tmp_path, "old", STALE, [], "x", "T01", started="2026-10-01T00:00:00")
    _trace(tmp_path, "new", STALE, [], "y", "T01", started="2026-10-23T00:00:00")
    rows, dupes = rows_from_traces(tmp_path)
    assert dupes == 1 and [r["run_id"] for r in rows] == ["new"]


def test_analyze_end_to_end_on_partial_fake(tmp_path):
    from mas_drift.agents.policy_fake import PolicyFake
    from mas_drift.infra.config import DEFAULT_CONFIG, ProviderConfig
    from mas_drift.infra.llm import build_llm
    from mas_drift.runner import cli
    import analyze

    cfg = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        'runs_dir = "runs"', f'runs_dir = "{(tmp_path / "runs").as_posix()}"')
    path = tmp_path / "c.toml"
    path.write_text(cfg, encoding="utf-8")
    llm = build_llm(ProviderConfig(name="fake", kind="fake", model="policy-fake", cache=False), None, raw=PolicyFake())
    cli.main(["--config", str(path), "--provider", "fake", "--n-tasks", "4", "--max-runs", "25"], llm=llm, log=lambda s: None)
    out = tmp_path / "rep"
    code = analyze.main(["--experiment", "fake", "--provider", "fake", "--runs-dir", str(tmp_path / "runs"),
                         "--out", str(out), "--n-boot", "500"])
    assert code == 0
    md = (out / "analysis.md").read_text(encoding="utf-8")
    # 25 прогонов при 10 условиях: 2 полных блока (20 прогонов), 5 прогонов неполного блока исключены.
    assert "Полных блоков (задача во всех 10 условиях сетки): 2**" in md
    assert "только полные блоки: **20** прогонов; исключено прогонов из неполных блоков: 5" in md
    assert "`v2_contract_stale:A0` = 2" in md and "Понял, но не сделал" in md
    assert (out / "success_by_condition.png").stat().st_size > 10_000
    code = analyze.main(["--experiment", "fake", "--provider", "fake", "--runs-dir", str(tmp_path / "runs"),
                         "--out", str(out), "--n-boot", "500", "--include-partial"])
    md = (out / "analysis.md").read_text(encoding="utf-8")
    assert code == 0 and "включены **все** завершённые прогоны (25)" in md and "Данные частичные" in md


def test_complete_blocks_only():
    from mas_drift.analysis import complete_blocks_only
    rows = [_row(c, t, True) for t in ("T1", "T2") for c in ("v1:A0", "v1:A1")] + [_row("v1:A0", "T3", True)]
    kept, complete, started = complete_blocks_only(rows, ["v1:A0", "v1:A1"])
    assert complete == {("T1", 0), ("T2", 0)} and len(started) == 3 and len(kept) == 4


def test_units_understood_not_done():
    from mas_drift.analysis import MENTION_UNITS, _unconverted
    recs = [{"type": "run_start"},
            {"type": "step", "agent": "executor", "tool_calls": [{"arguments": "{}"}],
             "tool_results": [{"result": [{"id": "H1", "price": 24450}]}, {"result": [{"id": "F1", "price": 18570}]}]}]
    raw_report = '{"hotels": [{"id": "H1", "price_usd": 24450.0}], "notes": "prices appear to be in cents"}'
    fixed_report = '{"hotels": [{"id": "H1", "price_usd": 244.50}], "flights": [{"id": "F1", "price_usd": 185.70}]}'
    assert _unconverted(recs, raw_report, '{"no_option": true}')
    assert not _unconverted(recs, fixed_report, '{"no_option": true}')
    assert _unconverted(recs, fixed_report, '{"hotel_id": "H1", "flight_id": "F1", "total_usd": 43020}')
    assert MENTION_UNITS.search(raw_report) and not MENTION_UNITS.search("50 percent of recent center")
