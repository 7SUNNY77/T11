"""Промпт LLM-судьи и разбор вердикта. Изменение текста — повод поднять JUDGE_PROMPT_VERSION."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

JUDGE_PROMPT_VERSION = "j1"

AGENTS = ("planner", "executor", "verifier")
CATEGORIES = ("wrong_tool_call", "misread_tool_output", "reasoning_or_math", "verification_failure", "gave_up", "other")

JUDGE_SYSTEM = """Ты — эксперт по отладке мультиагентных LLM-систем. Тебе дают сжатую трассу одного прогона
системы бронирования командировок, который завершился НЕВЕРНЫМ финальным ответом.

Система состоит из трёх агентов, работающих последовательно:
- planner — получает запрос пользователя и выдаёт структурированный план (города, дата, число ночей,
  бюджет). Инструменты не вызывает.
- executor — получает запрос и план, вызывает инструменты search_hotels (отели в городе; цена — за всё
  проживание) и search_flights (рейсы на дату), видит их ответы и пишет отчёт для verifier: самые
  дешёвые отели и рейсы с ценами в долларах США и лучшую комбинацию. Бюджет executor не проверяет.
- verifier — видит ТОЛЬКО запрос, план и отчёт executor (ответов инструментов он не видит), проверяет
  бюджет и выдаёт финальный ответ: {"hotel_id", "flight_id", "total_usd"} или {"no_option": true}.

Задача: найди решающую ошибку — САМУЮ РАННЮЮ ошибку в трассе, без которой прогон был бы успешным, —
укажи агента и номер шага ([шаг N]) и объясни причину. Ориентируйся на эталонный ответ и на то, что
каждый агент мог знать на своём шаге.

Категории ошибки:
- wrong_tool_call — неверный вызов инструмента (параметры, их имена или значения);
- misread_tool_output — ответ инструмента прочитан или интерпретирован неверно;
- reasoning_or_math — ошибка рассуждения или арифметики при верно прочитанных данных;
- verification_failure — verifier пропустил или внёс ошибку при проверке;
- gave_up — агент прекратил попытки, не исчерпав очевидных вариантов исправления;
- other — иное.

Ответь строго одним JSON-объектом без другого текста:
{"blamed_agent": "planner|executor|verifier", "blamed_step": <номер шага>, "error_category": "<категория>",
 "root_cause": "<1–2 предложения>", "confidence": <число от 0 до 1>}"""


class Verdict(BaseModel):
    blamed_agent: Literal["planner", "executor", "verifier"]
    blamed_step: int
    error_category: Literal[
        "wrong_tool_call", "misread_tool_output", "reasoning_or_math", "verification_failure", "gave_up", "other"
    ]
    root_cause: str
    confidence: float = Field(ge=0, le=1)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def parse_verdict(text: str | None) -> tuple[Verdict | None, str | None]:
    """(вердикт, None) или (None, описание ошибки разбора)."""
    if not text:
        return None, "пустой ответ"
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        return None, f"не JSON: {exc}"
    try:
        return Verdict.model_validate(obj), None
    except ValidationError as exc:
        return None, f"схема: {exc.errors(include_url=False)[0]['msg']}"


def judge_messages(compressed_text: str) -> list[dict]:
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": compressed_text}]
