"""Истина для оценки судьи — вычисляется из трассы и судье не показывается.

Виноватый агент — executor. Два шага:
- drift_entry_step — первый шаг Executor, на котором он получил результат инструмента с
  drift_affected = true или ошибку инструмента (где дрейф вошёл в систему);
- agent_error_step — первый шаг, где агент ДЕЙСТВОВАЛ на основе дрейфа. Для отклонённого вызова
  это шаг, на котором отправлен вызов, получивший ошибку (вызов и его результат — один шаг трассы).
  Для изменения выхода (центы, переименованные поля) — следующий шаг Executor после получения
  результата: на нём агент читает изменённые данные.
Для условий без дрейфа истины нет.
"""

from __future__ import annotations

from mas_drift.tools.errors import is_error

# v1 — без изменений API; v2_benign — обратно совместимые изменения: провал там дрейфом не объясняется.
NO_DRIFT_VERSIONS = {"v1", "v2_benign"}


def ground_truth(records: list[dict]) -> dict:
    api = records[0]["condition"]["api_version"]
    if api in NO_DRIFT_VERSIONS:
        return {"status": "no_ground_truth", "reason": f"условие без дрейфа ({api})"}
    executor_steps = [r for r in records if r.get("type") == "step" and r.get("agent") == "executor"]
    for i, r in enumerate(executor_steps):
        flagged = [tr for tr in r["tool_results"] if tr["drift_affected"] or is_error(tr["result"])]
        if not flagged:
            continue
        # Все сигналы шага (параллельные вызовы): например, рейсы с output_renamed и 422 по отелям.
        kinds = sorted({"tool_error" if is_error(tr["result"]) else tr["drift_type"] for tr in flagged})
        entry = r["step"]
        if any(is_error(tr["result"]) for tr in flagged):
            error_step, rule = entry, "rejected_call"
        elif i + 1 < len(executor_steps):
            error_step, rule = executor_steps[i + 1]["step"], "next_executor_step"
        else:
            error_step, rule = entry, "no_next_executor_step"
        return {"status": "ok", "agent": "executor", "drift_entry_step": entry, "agent_error_step": error_step,
                "rule": rule, "trigger": "+".join(kinds)}
    return {"status": "no_ground_truth", "reason": "в трассе нет результата с дрейфом или ошибкой"}
