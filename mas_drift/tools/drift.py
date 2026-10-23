"""Разметка истины для трасс: затронут ли ответ инструмента дрейфом API.

Каждый вызов параллельно выполняется на v1 с теми же аргументами; ответы сравниваются.
Агенты видят только `result`; поля drift_* и shadow_v1_result пишутся лишь в трассу.

drift_type:
  none                  — ответ идентичен v1;
  input_rejected        — v1 принимает такой вызов, текущая версия отвергает;
  input_v2_only         — текущая версия принимает вызов, v1 бы его отвергла (агент уже под v2);
  error_changed         — обе версии вернули ошибку, но разную;
  output_renamed        — у элементов пропали поля v1 и появились новые;
  output_removed_fields — у элементов пропали поля v1;
  output_units          — те же поля, числовые значения отличаются ровно в 100 раз;
  output_values_changed — те же поля, значения отличаются иначе;
  output_added_fields   — добавлены поля, общие значения совпадают;
  results_changed       — другой набор или порядок элементов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from mas_drift.tools.errors import is_error
from mas_drift.tools.versions import ToolSet, get_toolset


class AnnotatedResult(BaseModel):
    result: Any
    drift_affected: bool
    drift_type: str
    shadow_v1_result: Any


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_units(cur: Any, base: Any) -> bool:
    return _is_number(cur) and _is_number(base) and math.isclose(cur, base * 100, abs_tol=0.5)


def classify(current: Any, v1: Any) -> str:
    if current == v1:
        return "none"
    cur_err, v1_err = is_error(current), is_error(v1)
    if cur_err and v1_err:
        return "error_changed"
    if cur_err:
        return "input_rejected"
    if v1_err:
        return "input_v2_only"
    if not (isinstance(current, list) and isinstance(v1, list)):
        return "results_changed"
    if [r.get("id") for r in current] != [r.get("id") for r in v1]:
        return "results_changed"

    missing = added = False
    changed: list[tuple[Any, Any]] = []
    for c, b in zip(current, v1):
        ck, bk = set(c), set(b)
        missing |= bool(bk - ck)
        added |= bool(ck - bk)
        changed += [(c[k], b[k]) for k in ck & bk if c[k] != b[k]]
    if missing:
        return "output_renamed" if added else "output_removed_fields"
    if changed:
        return "output_units" if all(_is_units(c, b) for c, b in changed) else "output_values_changed"
    if added:
        return "output_added_fields"
    return "results_changed"


@dataclass
class DriftObservedToolSet:
    """Текущая версия API + теневая v1 для разметки."""

    current: ToolSet
    shadow: ToolSet

    @classmethod
    def for_version(cls, version: str, world) -> "DriftObservedToolSet":
        return cls(get_toolset(version, world), get_toolset("v1", world))

    @property
    def version(self) -> str:
        return self.current.version

    def schemas(self) -> list[dict]:
        return self.current.schemas()

    def call(self, name: str, args: dict) -> AnnotatedResult:
        result = self.current.call(name, args)
        v1 = self.shadow.call(name, args)
        kind = classify(result, v1)
        return AnnotatedResult(
            result=result, drift_affected=kind != "none", drift_type=kind, shadow_v1_result=v1
        )
