"""Детерминированная проверка финального ответа МАС по эталону задачи.

Финальный ответ должен быть ровно одним JSON-объектом. Допускаются окружающие пробелы и
обёртка ```json ... ``` (отмечается флагом json_fenced); всё остальное — invalid_json.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel

from mas_drift.domain.tasks import Task

TOTAL_TOLERANCE_USD = 1.0

Reason = Literal[
    "invalid_json",      # не парсится как один JSON-объект
    "schema_error",      # JSON, но не {"hotel_id","flight_id","total_usd"} и не {"no_option": true}
    "missed_no_option",  # решения нет, а система предложила пару
    "false_no_option",   # решение есть, а система ответила no_option
    "wrong_pair",        # пара не входит в множество эталонных
    "wrong_total",       # пара верная, сумма вне ±1 USD
]

_FENCE = re.compile(r"^```(?:json|JSON)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


class CheckResult(BaseModel):
    success: bool
    reason: Reason | None
    gold_kind: str
    answer_kind: Literal["pair", "no_option"] | None = None
    pair_ok: bool = False
    total_ok: bool = False
    json_fenced: bool = False
    parsed: Any = None


def parse_final_json(text: str | None) -> tuple[Any, bool]:
    """Возвращает (объект или None, json_fenced)."""
    if text is None:
        return None, False
    s = text.strip()
    fenced = False
    m = _FENCE.match(s)
    if m:
        s, fenced = m.group(1).strip(), True
    try:
        return json.loads(s), fenced
    except (json.JSONDecodeError, ValueError):
        return None, fenced


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check(task: Task, final_text: str | None) -> CheckResult:
    gold = task.gold
    obj, fenced = parse_final_json(final_text)
    base = dict(gold_kind=gold.kind, json_fenced=fenced, parsed=obj)

    if not isinstance(obj, dict):
        return CheckResult(success=False, reason="invalid_json", **base)

    if obj.get("no_option") is True:
        if gold.kind == "no_option":
            return CheckResult(success=True, reason=None, answer_kind="no_option", **base)
        return CheckResult(success=False, reason="false_no_option", answer_kind="no_option", **base)

    hid, fid, total = obj.get("hotel_id"), obj.get("flight_id"), obj.get("total_usd")
    if not (isinstance(hid, str) and isinstance(fid, str) and _is_number(total)):
        return CheckResult(success=False, reason="schema_error", **base)

    if gold.kind == "no_option":
        return CheckResult(success=False, reason="missed_no_option", answer_kind="pair", **base)

    pair_ok = (hid.strip(), fid.strip()) in gold.pair_set()
    # Сравнение в целых центах: граница ±1.00 USD не зависит от погрешности float.
    total_ok = abs(round(float(total) * 100) - round(gold.total_usd * 100)) <= round(TOTAL_TOLERANCE_USD * 100)
    reason: Reason | None = None if pair_ok and total_ok else ("wrong_pair" if not pair_ok else "wrong_total")
    return CheckResult(
        success=reason is None, reason=reason, answer_kind="pair", pair_ok=pair_ok, total_ok=total_ok, **base
    )
