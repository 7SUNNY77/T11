import json

import pytest

from mas_drift.domain.tasks import Gold, Task
from mas_drift.eval import check, parse_final_json


def _task(gold: Gold) -> Task:
    return Task(
        task_id="T00", origin="A", city="B", date="2026-11-02", nights=2, budget=500,
        request="...", gold=gold, designed_as="plain",
    )


PAIRS = _task(Gold(kind="pairs", pairs=[("H1", "F1"), ("H2", "F1")], total_usd=300.40))
NONE = _task(Gold(kind="no_option"))


def _ans(**kw) -> str:
    return json.dumps(kw)


@pytest.mark.parametrize("hid", ["H1", "H2"])
def test_success_any_tied_pair(hid):
    r = check(PAIRS, _ans(hotel_id=hid, flight_id="F1", total_usd=300.40))
    assert r.success and r.reason is None and r.pair_ok and r.total_ok and r.answer_kind == "pair"


@pytest.mark.parametrize("total,ok", [(301.40, True), (299.40, True), (301.41, False), (299.39, False), (300, True)])
def test_total_tolerance(total, ok):
    r = check(PAIRS, _ans(hotel_id="H1", flight_id="F1", total_usd=total))
    assert r.success is ok
    assert r.pair_ok
    if not ok:
        assert r.reason == "wrong_total" and not r.total_ok


def test_wrong_pair():
    r = check(PAIRS, _ans(hotel_id="H3", flight_id="F1", total_usd=300.40))
    assert not r.success and r.reason == "wrong_pair" and not r.pair_ok and r.total_ok


def test_units_like_failure_marks_pair_ok_total_wrong():
    r = check(PAIRS, _ans(hotel_id="H1", flight_id="F1", total_usd=30040))
    assert r.reason == "wrong_total" and r.pair_ok and not r.total_ok


def test_no_option_cases():
    assert check(NONE, _ans(no_option=True)).success
    r = check(NONE, _ans(hotel_id="H1", flight_id="F1", total_usd=1))
    assert r.reason == "missed_no_option"
    r = check(PAIRS, _ans(no_option=True))
    assert r.reason == "false_no_option" and r.answer_kind == "no_option"


def test_no_option_false_falls_back_to_pair():
    r = check(PAIRS, _ans(no_option=False, hotel_id="H1", flight_id="F1", total_usd=300.4))
    assert r.success


@pytest.mark.parametrize(
    "text",
    [None, "", "not json", '{"hotel_id": "H1"', "[1, 2]", '"H1"',
     'Answer: {"hotel_id": "H1", "flight_id": "F1", "total_usd": 300.4}'],
)
def test_invalid_json(text):
    r = check(PAIRS, text)
    assert not r.success and r.reason == "invalid_json"


@pytest.mark.parametrize(
    "obj",
    [{"hotel_id": "H1", "flight_id": "F1"},
     {"hotel_id": "H1", "flight_id": "F1", "total_usd": "300.40"},
     {"hotel_id": 1, "flight_id": "F1", "total_usd": 300.4},
     {"hotel_id": "H1", "flight_id": "F1", "total_usd": True},
     {"no_option": "yes"}],
)
def test_schema_error(obj):
    r = check(PAIRS, json.dumps(obj))
    assert not r.success and r.reason == "schema_error"


def test_fenced_json_accepted_and_flagged():
    body = _ans(hotel_id="H1", flight_id="F1", total_usd=300.4)
    for text in (f"```json\n{body}\n```", f"  ```\n{body}```  ", f"```JSON {body} ```"):
        r = check(PAIRS, text)
        assert r.success and r.json_fenced, text
    assert not check(PAIRS, body).json_fenced


def test_parse_final_json_whitespace():
    assert parse_final_json('\n {"no_option": true} \n') == ({"no_option": True}, False)
