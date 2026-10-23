import json
from collections import Counter

import pytest

from mas_drift.agents import Condition, run_mas
from mas_drift.agents import prompts
from mas_drift.agents.jsonutil import extract_json
from mas_drift.agents.policy_fake import PolicyFake
from mas_drift.domain import generate_tasks, generate_world
from mas_drift.eval import check
from mas_drift.infra.fake import ScriptedFake, text, tool_calls
from mas_drift.infra.trace import TraceWriter, read_trace, trace_path
from mas_drift.tools import API_VERSIONS, changelog_for
from mas_drift.tools.changelogs import IRRELEVANT_CHANGELOG

A0 = []
A1 = ["executor"]


@pytest.fixture(scope="module")
def world():
    return generate_world(42)


@pytest.fixture(scope="module")
def tasks(world):
    return generate_tasks(world, 30, 42)


def _run(tmp_path, task, world, llm, version="v1", changelog_to=A0, name="r", max_steps=8):
    cond = Condition(api_version=version, awareness="A?", changelog_to=changelog_to)
    path = trace_path(tmp_path, name)
    with TraceWriter(path, {"run_id": name}) as tw:
        res = run_mas(task, cond, world, llm, tw, repeat_idx=0, executor_max_steps=max_steps)
        tw.finish()
    return res, read_trace(path)


def _plan_json(task):
    return json.dumps({"origin": task.origin, "destination": task.city, "date": task.date,
                       "nights": task.nights, "budget_usd": task.budget})


def test_executor_prompt_has_v1_few_shot():
    assert '"nights": 2' in prompts.EXECUTOR_SYSTEM and '"price": 180.0' in prompts.EXECUTOR_SYSTEM
    assert "num_nights" not in prompts.EXECUTOR_SYSTEM


@pytest.mark.parametrize("changelog_to", [[], ["executor"], ["executor", "verifier"], ["planner"]])
def test_changelog_recipients(tmp_path, world, tasks, changelog_to):
    llm = PolicyFake()
    seen = []
    orig = llm.chat

    def spy(messages, tools=None, *, repeat_idx=0):
        seen.append(messages[0]["content"])
        return orig(messages, tools, repeat_idx=repeat_idx)

    llm.chat = spy
    _run(tmp_path, tasks[0], world, llm, version="v2_units", changelog_to=changelog_to)
    roles = {"planner": prompts.PLANNER_ROLE, "executor": prompts.EXECUTOR_ROLE, "verifier": prompts.VERIFIER_ROLE}
    for agent, role in roles.items():
        systems = [s for s in seen if s.startswith(role)]
        assert systems
        for s in systems:
            assert (changelog_for("v2_units") in s) == (agent in changelog_to)


def test_v1_a1_gets_irrelevant_changelog():
    cond = Condition(api_version="v1", awareness="A1", changelog_to=A1)
    assert cond.changelog_for_agent("executor") == IRRELEVANT_CHANGELOG
    assert cond.changelog_for_agent("verifier") is None


def test_executor_parallel_tools_and_hidden_drift(tmp_path, world, tasks):
    t = tasks[0]
    script = [
        text(_plan_json(t)),
        tool_calls(("search_hotels", {"city": t.city, "nights": t.nights}),
                   ("search_flights", {"origin": t.origin, "destination": t.city, "date": t.date})),
        text('{"best": null}'),
        text('{"no_option": true}'),
    ]
    llm = ScriptedFake(script)
    res, trace = _run(tmp_path, t, world, llm, version="v2_units")
    assert res.executor_status == "ok" and res.stats.llm_calls == 4 and res.stats.tool_calls == 2
    assert res.stats.drift_affected_results == 2
    # Модель видит tool-сообщения без служебной разметки, в порядке вызовов.
    third_call = llm.calls[2]["messages"]
    tool_msgs = [m for m in third_call if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1"]
    assert all("drift" not in m["content"] and "shadow" not in m["content"] for m in tool_msgs)
    # В трассе разметка есть.
    ex_steps = [r for r in trace if r.get("agent") == "executor"]
    results = ex_steps[0]["tool_results"]
    assert [r["drift_type"] for r in results] == ["output_units", "output_units"]
    assert all(r["drift_affected"] and r["shadow_v1_result"] for r in results)
    assert ex_steps[1]["tool_results"] == []
    assert [r["agent"] for r in trace if r["type"] == "step"] == ["planner", "executor", "executor", "verifier"]


def test_executor_step_limit(tmp_path, world, tasks):
    t = tasks[0]
    loop = tool_calls(("search_flights", {"origin": t.origin, "destination": t.city, "date": t.date}))
    llm = ScriptedFake([text("{}")] + [loop] * 8 + [text('{"no_option": true}')])
    res, trace = _run(tmp_path, t, world, llm)
    assert res.executor_status == "step_limit" and res.executor_steps == 8
    assert res.stats.calls_by_agent == {"planner": 1, "executor": 8, "verifier": 1}
    assert prompts.EXECUTOR_STEP_LIMIT_REPORT in llm.calls[-1]["messages"][1]["content"]


def test_executor_bad_tool_arguments(tmp_path, world, tasks):
    t = tasks[0]
    bad = tool_calls(("search_hotels", {}))
    bad.tool_calls[0].arguments = "{city: oops"
    llm = ScriptedFake([text("{}"), bad, text("done"), text('{"no_option": true}')])
    res, trace = _run(tmp_path, t, world, llm, version="v2_contract")
    tr = [r for r in trace if r.get("agent") == "executor"][0]["tool_results"][0]
    assert tr["arguments_parsed"] is False and tr["result"]["status"] == 400
    assert tr["drift_affected"] is False
    assert res.stats.tool_errors == 1


def test_extract_json():
    assert extract_json('Here: {"a": 1} trailing') == {"a": 1}
    assert extract_json("```json\n[1,2]\n```") == [1, 2]
    assert extract_json("{bad} {\"b\": 2}") == {"b": 2}
    assert extract_json("none") is None


EXPECTED_ACCURACY = {
    # (версия, получает ли Executor changelog) -> успех на задачах с решением / без решения
    ("v1", False): (1.0, 1.0), ("v1", True): (1.0, 1.0),
    ("v2_benign", False): (1.0, 1.0), ("v2_benign", True): (1.0, 1.0),
    ("v2_contract", False): (0.0, 1.0), ("v2_contract", True): (1.0, 1.0),
    ("v2_contract_stale", False): (0.0, 1.0), ("v2_contract_stale", True): (1.0, 1.0),
    ("v2_units", False): (0.0, 1.0), ("v2_units", True): (1.0, 1.0),
}


@pytest.mark.parametrize("version", API_VERSIONS)
@pytest.mark.parametrize("aware", [False, True])
def test_policy_fake_expected_matrix(tmp_path, world, tasks, version, aware):
    counts = Counter()
    reasons = Counter()
    for t in tasks:
        res, _ = _run(tmp_path, t, world, PolicyFake(), version=version, changelog_to=A1 if aware else A0,
                      name=f"{version}_{aware}_{t.task_id}")
        c = check(t, res.final_text)
        counts[(t.gold.kind, c.success)] += 1
        if not c.success:
            reasons[c.reason] += 1
    solvable = counts[("pairs", True)] / (counts[("pairs", True)] + counts[("pairs", False)])
    no_opt = counts[("no_option", True)] / (counts[("no_option", True)] + counts[("no_option", False)])
    assert (solvable, no_opt) == EXPECTED_ACCURACY[(version, aware)], reasons
    if (solvable, aware) == (0.0, False):
        assert set(reasons) == {"false_no_option"}
