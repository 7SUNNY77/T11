import csv
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from mas_drift.agents import Condition
from mas_drift.agents.policy_fake import PolicyFake
from mas_drift.domain import generate_tasks, generate_world
from mas_drift.infra.cache import CachedLLM, DiskCache
from mas_drift.infra.config import DEFAULT_CONFIG, ProviderConfig
from mas_drift.infra.errors import DailyQuotaExceeded
from mas_drift.infra.fake import ScriptedFake, text
from mas_drift.infra.llm import LLMClient, LLMResponse, build_llm
from mas_drift.infra.trace import read_trace, trace_path
from mas_drift.runner import cli
from mas_drift.runner.grid import build_grid, experiment_dir, make_run_id, run_setup

N_TASKS = 30
VERSIONS = ["v1", "v2_contract", "v2_contract_stale", "v2_units", "v2_benign"]
N_CONDS = 2 * len(VERSIONS)
ALL_CONDS = [f"{v}:{a}" for v in VERSIONS for a in ("A0", "A1")]


@pytest.fixture
def config(tmp_path) -> Path:
    text_ = DEFAULT_CONFIG.read_text(encoding="utf-8")
    text_ = text_.replace('runs_dir = "runs"', f'runs_dir = "{(tmp_path / "runs").as_posix()}"')
    text_ = text_.replace('cache_dir = "cache"', f'cache_dir = "{(tmp_path / "cache").as_posix()}"')
    path = tmp_path / "config.toml"
    path.write_text(text_, encoding="utf-8")
    return path


class Counting(LLMClient):
    """PolicyFake со счётчиком вызовов и опциональным «дневным лимитом» после N вызовов."""

    def __init__(self, quota_after: int | None = None):
        self.inner = PolicyFake()
        self.provider, self.model, self.temperature = "fake", "policy-fake", 0.0
        self.calls = 0
        self.quota_after = quota_after
        self.lock = threading.Lock()

    def chat(self, messages, tools=None, *, repeat_idx=0):
        with self.lock:
            self.calls += 1
            if self.quota_after is not None and self.calls > self.quota_after:
                raise DailyQuotaExceeded("requests per day exceeded")
        return self.inner.chat(messages, tools, repeat_idx=repeat_idx)


def _run(config, *extra, llm=None, workers=3):
    pcfg = ProviderConfig(name="fake", kind="fake", model="policy-fake", cache=False)
    raw = llm or Counting()
    code = cli.main(["--config", str(config), "--provider", "fake", "--workers", str(workers), *extra],
                    llm=build_llm(pcfg, None, raw=raw), log=lambda s: None)
    return code, raw


def _exp_dir(config, name="fake"):
    return config.parent / "runs" / name / "fake"


def _results(config, name="fake"):
    with open(_exp_dir(config, name) / "results.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


EXPECTED = {  # (solvable, no_option) успехи на 25 + 5 задачах
    "v1:A0": (25, 5), "v1:A1": (25, 5), "v2_benign:A0": (25, 5), "v2_benign:A1": (25, 5),
    "v2_contract:A0": (0, 5), "v2_contract:A1": (25, 5), "v2_units:A0": (0, 5), "v2_units:A1": (25, 5),
    "v2_contract_stale:A0": (0, 5), "v2_contract_stale:A1": (25, 5),
}


def test_full_grid_end_to_end(config):
    code, llm = _run(config, "--conditions", "all", "--n-tasks", "30", "--repeats", "1")
    assert code == 0
    rows = _results(config)
    assert len(rows) == N_TASKS * N_CONDS
    got = Counter()
    for r in rows:
        got[(f"{r['api_version']}:{r['awareness']}", r["gold_kind"])] += r["success"] == "True"
    for cid, (solv, noopt) in EXPECTED.items():
        assert (got[(cid, "pairs")], got[(cid, "no_option")]) == (solv, noopt), cid
    # Фактические LLM-вызовы, модель, reasoning_effort и время начала есть в каждой строке.
    assert all(int(r["llm_calls"]) == 4 for r in rows)
    assert all(r["model"] == "policy-fake" and r["started_at"] and r["llm_latency_s"] != "" for r in rows)
    assert llm.calls == N_TASKS * N_CONDS * 4
    summary = (_exp_dir(config) / "summary.csv").read_text(encoding="utf-8")
    assert "v2_units:A0" in summary


def test_paired_design_same_tasks_in_all_conditions(config):
    _run(config, "--n-tasks", "30")
    rows = _results(config)
    by_cond = defaultdict(set)
    for r in rows:
        by_cond[f"{r['api_version']}:{r['awareness']}"].add((r["task_id"], r["repeat_idx"]))
    task_sets = list(by_cond.values())
    assert set(by_cond) == set(ALL_CONDS)
    assert all(s == task_sets[0] for s in task_sets) and len(task_sets[0]) == N_TASKS
    # Эталон задачи одинаков во всех условиях.
    gold = defaultdict(set)
    for r in rows:
        gold[r["task_id"]].add((r["gold_kind"], r["designed_as"]))
    assert all(len(v) == 1 for v in gold.values())


def test_changelog_actually_in_prompts(config):
    _run(config, "--n-tasks", "3")
    for r in _results(config):
        expected = "none" if r["awareness"] == "A0" else ("irrelevant" if r["api_version"] == "v1" else "own")
        assert r["changelog_executor"] == expected, r["run_id"]
        assert r["changelog_planner"] == "none" and r["changelog_verifier"] == "none"


def test_order_blocks_balanced_and_seeded():
    world = generate_world(42)
    tasks = generate_tasks(world, 30, 42)[:6]
    conds = [Condition(api_version=c.split(":")[0], awareness=c.split(":")[1],
                       changelog_to=[] if c.endswith("A0") else ["executor"]) for c in ALL_CONDS]
    setup = {"x": 1}
    specs = build_grid(conds, tasks, repeats=2, setup=setup, order_seed=7)
    assert len(specs) == 6 * N_CONDS * 2
    # Каждый блок — одна задача × все 8 условий, блоки идут подряд.
    for b in range(12):
        block = specs[b * N_CONDS:(b + 1) * N_CONDS]
        assert {s.block for s in block} == {b}
        assert len({s.task.task_id for s in block}) == 1
        assert {s.condition.id for s in block} == set(ALL_CONDS)
    # Порядок перемешан, но воспроизводим.
    again = build_grid(conds, tasks, repeats=2, setup=setup, order_seed=7)
    other = build_grid(conds, tasks, repeats=2, setup=setup, order_seed=8)
    assert [s.run_id for s in again] == [s.run_id for s in specs]
    assert [s.run_id for s in other] != [s.run_id for s in specs]
    assert [s.condition.id for s in specs[:N_CONDS]] != [s.condition.id for s in specs[N_CONDS:2 * N_CONDS]]
    assert [s.task.task_id for s in specs[::N_CONDS][:6]] != [t.task_id for t in tasks]
    # Любой префикс сбалансирован по условиям с точностью до одного блока.
    for cut in range(0, len(specs), 5):
        counts = Counter(s.condition.id for s in specs[:cut])
        assert max(counts.values(), default=0) - min((counts.get(c, 0) for c in ALL_CONDS), default=0) <= 1


def test_resume_skips_completed_and_reruns_missing(config):
    _run(config, "--n-tasks", "4")
    code, llm = _run(config, "--n-tasks", "4")
    assert code == 0 and llm.calls == 0
    victim = next((_exp_dir(config) / "traces").glob("*.jsonl"))
    victim.unlink()
    code, llm = _run(config, "--n-tasks", "4")
    assert llm.calls == 4 and len(_results(config)) == 4 * N_CONDS


def test_daily_quota_stops_and_resume_completes(config):
    code, llm = _run(config, "--n-tasks", "5", llm=Counting(quota_after=50))
    assert code == cli.EXIT_RESUME_LATER
    partial = _results(config)
    assert 0 < len(partial) < 5 * N_CONDS
    parts = list((_exp_dir(config) / "traces").glob("*.part"))
    assert parts and all(read_trace(p)[-1]["type"] == "run_aborted" for p in parts)
    code, llm = _run(config, "--n-tasks", "5")
    assert code == 0 and llm.calls == (5 * N_CONDS - len(partial)) * 4
    rows = _results(config)
    assert len(rows) == 5 * N_CONDS and not list((_exp_dir(config) / "traces").glob("*.part"))


def test_workers_do_not_change_results(config, tmp_path):
    _run(config, "--n-tasks", "6", workers=1)
    seq = {r["run_id"]: (r["success"], r["reason"]) for r in _results(config)}
    _run(config, "--n-tasks", "6", "--experiment", "fake-par", workers=6)
    par = {r["run_id"]: (r["success"], r["reason"]) for r in _results(config, "fake-par")}
    assert seq == par


def test_trace_header_fields(config):
    _run(config, "--tasks", "T01", "--conditions", "v1:A0")
    path = next((_exp_dir(config) / "traces").glob("*.jsonl"))
    start, end = read_trace(path)[0], read_trace(path)[-1]
    for key in ("model", "reasoning_effort", "temperature", "prompt_version", "started_at", "condition",
                "task_id", "repeat_idx", "gold", "tool_schemas", "world_fp", "tasks_fp", "api_fp"):
        assert key in start, key
    assert end["stats"]["llm_calls"] == 4 and "llm_latency_s" in end["stats"]


def test_fake_isolation():
    fake = ProviderConfig(name="fake", kind="fake", model="m")
    real = ProviderConfig(name="gemini", kind="openai_compat", model="m")
    with pytest.raises(ValueError):
        experiment_dir(Path("runs"), "stage1", "fake", fake)
    with pytest.raises(ValueError):
        experiment_dir(Path("runs"), "fake", "fake", real)
    assert experiment_dir(Path("runs"), "fake-x", "fake", fake) == Path("runs/fake-x/fake")


def test_run_id_depends_on_reasoning_effort():
    cond = Condition(api_version="v1", awareness="A0", changelog_to=[])
    base = ProviderConfig(name="g", kind="openai_compat", model="m", reasoning_effort="low")
    s1 = run_setup(base, 8, "w", "t")
    s2 = run_setup(base.model_copy(update={"reasoning_effort": "medium"}), 8, "w", "t")
    assert make_run_id(s1, cond, "T01", 0) != make_run_id(s2, cond, "T01", 0)
    assert make_run_id(s1, cond, "T01", 0) == make_run_id(s1, cond, "T01", 0)


def test_estimate_uses_experiment_traces(config, capsys):
    _run(config, "--n-tasks", "2")
    lines = []
    code = cli.main(["--config", str(config), "--provider", "fake", "--n-tasks", "4", "--estimate"],
                    log=lines.append)
    out = "\n".join(lines)
    assert code == 0 and f"runs_remaining: {2 * N_CONDS}" in out and "трассы эксперимента" in out


def test_cache_in_flight_dedupe(tmp_path):
    class Slow(ScriptedFake):
        def chat(self, messages, tools=None, *, repeat_idx=0):
            time.sleep(0.2)
            return super().chat(messages, tools, repeat_idx=repeat_idx)

    inner = Slow([text("a"), text("b")])
    llm = CachedLLM(inner, DiskCache(tmp_path))
    out: list[LLMResponse] = []
    threads = [threading.Thread(target=lambda: out.append(llm.chat([{"role": "user", "content": "x"}])))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(inner.calls) == 1 and {r.content for r in out} == {"a"}


def test_adding_version_keeps_old_run_ids(monkeypatch):
    from mas_drift.tools import versions

    cond = Condition(api_version="v1", awareness="A0", changelog_to=[])
    setup = run_setup(ProviderConfig(name="g", kind="openai_compat", model="m"), 8, "w", "t")
    before = make_run_id(setup, cond, "T01", 0)
    monkeypatch.setitem(versions.SPECS, "v9_new", versions.SPECS["v2_units"])
    assert make_run_id(setup, cond, "T01", 0) == before


def test_cache_hits_column(config):
    _run(config, "--tasks", "T01", "--conditions", "v1:A0,v2_units:A0", workers=1)
    rows = {r["api_version"]: r for r in _results(config)}
    # Fake без кэша: попаданий нет, столбец есть.
    assert rows["v1"]["cache_hits"] == "0" and "cached_calls" not in rows["v1"]


def test_cache_hits_shared_planner_with_cache(config, tmp_path):
    pcfg = ProviderConfig(name="fake", kind="fake", model="policy-fake", cache=True)
    code = cli.main(["--config", str(config), "--provider", "fake", "--workers", "1", "--tasks", "T01",
                     "--conditions", "v1:A0,v1:A1,v2_units:A0"],
                    llm=build_llm(pcfg, tmp_path / "c", raw=Counting()), log=lambda s: None)
    assert code == 0
    hits = {r["api_version"] + ":" + r["awareness"]: int(r["cache_hits"]) for r in _results(config)}
    # Первый по порядку прогон всё вызывает сам. Остальные делят с ним дословно одинаковые вызовы:
    # Planner — всегда; v2_units:A0 с v1:A0 — первый шаг Executor (схемы совпадают);
    # v1:A1 с v1:A0 — Verifier (у fake отчёт Executor одинаковый, значит и вход Verifier).
    assert sorted(hits.values()) == [0, 2, 2] and sum(hits.values()) == 4
