"""Истина для оценки судьи — вычисляется из трассы и судье не показывается.

Виноватый агент — executor; истинный шаг — первый шаг Executor, на котором он получил результат
инструмента с drift_affected = true или ошибку инструмента. Для условий без дрейфа истины нет.
"""

from __future__ import annotations

from mas_drift.tools.errors import is_error

# v1 — без изменений API; v2_benign — обратно совместимые изменения: провал там дрейфом не объясняется.
NO_DRIFT_VERSIONS = {"v1", "v2_benign"}


def ground_truth(records: list[dict]) -> dict:
    api = records[0]["condition"]["api_version"]
    if api in NO_DRIFT_VERSIONS:
        return {"status": "no_ground_truth", "reason": f"условие без дрейфа ({api})"}
    for r in records:
        if r.get("type") == "step" and r.get("agent") == "executor":
            # Все сигналы шага (параллельные вызовы): например, рейсы с output_renamed и 422 по отелям.
            kinds = sorted({"tool_error" if is_error(tr["result"]) else tr["drift_type"]
                            for tr in r["tool_results"] if tr["drift_affected"] or is_error(tr["result"])})
            if kinds:
                return {"status": "ok", "agent": "executor", "step": r["step"], "trigger": "+".join(kinds)}
    return {"status": "no_ground_truth", "reason": "в трассе нет результата с дрейфом или ошибкой"}
