"""Единственное место, где формулируются ошибки mock-API.

Агент видит эти тексты как результат инструмента, поэтому их информативность — потенциальный
фактор эксперимента. Сейчас стиль один: подробная ошибка валидации в духе FastAPI 422.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError


def is_error(result: Any) -> bool:
    return isinstance(result, dict) and "error" in result


def validation_error(exc: ValidationError) -> dict:
    detail = []
    for e in exc.errors(include_url=False):
        item = {"type": e["type"], "loc": ["body", *map(str, e["loc"])], "msg": e["msg"]}
        if e["type"] != "missing":
            item["input"] = e.get("input")
        detail.append(item)
    return {"error": "Unprocessable Entity", "status": 422, "detail": detail}


def unknown_tool_error(name: str, known: list[str]) -> dict:
    return {
        "error": "Not Found",
        "status": 404,
        "detail": f"Unknown tool {name!r}. Available tools: {', '.join(known)}",
    }


def bad_arguments_json_error(raw: str) -> dict:
    return {
        "error": "Bad Request",
        "status": 400,
        "detail": "Tool arguments are not a valid JSON object",
        "input": raw[:500],
    }
