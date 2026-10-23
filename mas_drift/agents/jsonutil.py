"""Нестрогое извлечение JSON из текста модели — для передачи данных между агентами.
(Финальный ответ проверяется строго в eval/checker.py.)"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str | None) -> Any:
    """Первый JSON-объект/массив, который удаётся разобрать, иначе None."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(text, i)
                return obj
            except json.JSONDecodeError:
                continue
    return None
