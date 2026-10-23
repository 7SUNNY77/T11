"""Сжатая трасса — вход LLM-судьи.

Судья видит: запрос, эталон (и то, что прогон провален), пронумерованные шаги в порядке трассы и
финальный ответ. Судья НЕ видит: условие и версию API, разметку дрейфа (drift_*/shadow_v1_result),
системные промпты агентов (там changelog), имя эксперимента. Номер шага = номер шага в трассе.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from mas_drift.tools.errors import is_error

MAX_ITEMS = 5
MAX_TEXT = 1500
TOKEN_BUDGET = 3000
FALLBACK_TEXT_LIMITS = (MAX_TEXT, 800, 400)  # если вход не влезает в бюджет, урезаем только тексты агентов


@dataclass
class Compressed:
    text: str
    steps: list[int]
    text_limit: int
    est_tokens: int


def estimate_tokens(text: str) -> int:
    """Грубая оценка до вызова (кириллица и JSON плотнее английского текста); фактическое число
    токенов берётся из usage ответа провайдера."""
    return round(len(text) / 3.2)


def _cut(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f" …[урезано, всего {len(text)} символов]"


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


def _tool_result(result) -> list[str]:
    if is_error(result):
        return [f"    ответ: ОШИБКА {_dump(result)}"]
    if isinstance(result, list):
        head = f"    ответ: список из {len(result)} элементов" + (
            f", показаны первые {MAX_ITEMS}:" if len(result) > MAX_ITEMS else ":")
        return [head] + [f"      {_dump(item)}" for item in result[:MAX_ITEMS]] if result else ["    ответ: [] (пусто)"]
    return [f"    ответ: {_cut(_dump(result), MAX_TEXT)}"]


def gold_line(gold: dict) -> str:
    if gold["kind"] == "no_option":
        return "no_option — в рамках бюджета подходящих вариантов нет."
    pairs = gold["pairs"]
    total = f"{gold['total_usd']:.2f} USD"
    if len(pairs) == 1:
        h, f = pairs[0]
        return f"hotel_id={h}, flight_id={f}, total_usd={total}"
    alts = ", ".join(f"({h}, {f})" for h, f in pairs)
    return f"любая из равноценных пар (hotel_id, flight_id): {alts}; total_usd={total}"


def _render(records: list[dict], text_limit: int) -> Compressed:
    start, end = records[0], records[-1]
    lines = ["ЗАПРОС ПОЛЬЗОВАТЕЛЯ:", start["request"], "",
             "ЭТАЛОННЫЙ ОТВЕТ (прогон провален: финальный ответ системы с эталоном не совпал):",
             gold_line(start["gold"]), "", "ШАГИ:"]
    steps = []
    for r in records:
        if r.get("type") != "step":
            continue
        n, agent = r["step"], r["agent"]
        steps.append(n)
        content = r["response"].get("content")
        if agent == "planner":
            lines += [f"[шаг {n}] planner → план:", _cut(content, text_limit)]
        elif agent == "executor" and r["tool_calls"]:
            lines.append(f"[шаг {n}] executor → вызывает инструменты:")
            if content and content.strip():
                lines.append(f"  сообщение: {_cut(content, text_limit)}")
            results = {tr["tool_call_id"]: tr for tr in r["tool_results"]}
            for tc in r["tool_calls"]:
                lines.append(f"  {tc['name']}({tc['arguments']})")
                tr = results.get(tc["id"])
                lines += _tool_result(tr["result"]) if tr else ["    ответ: (нет)"]
        elif agent == "executor":
            lines += [f"[шаг {n}] executor → отчёт для verifier:", _cut(content, text_limit)]
        else:
            if end.get("executor_status") == "step_limit":
                # В порядке трассы: между последним шагом executor и verifier.
                lines += ["[система] executor исчерпал лимит шагов и не сформировал отчёт; verifier получил "
                          "сообщение об этом вместо отчёта.", ""]
            lines += [f"[шаг {n}] verifier → ответ:", _cut(content, text_limit)]
        lines.append("")
    lines += ["ФИНАЛЬНЫЙ ОТВЕТ СИСТЕМЫ:", _cut(end.get("final_text"), text_limit)]
    text = "\n".join(lines)
    return Compressed(text=text, steps=steps, text_limit=text_limit, est_tokens=estimate_tokens(text))


def compress_trace(records: list[dict]) -> Compressed:
    for limit in FALLBACK_TEXT_LIMITS:
        c = _render(records, limit)
        if c.est_tokens <= TOKEN_BUDGET:
            return c
    return c  # не влезло и при минимальном лимите — отдаём как есть, размер логируется


def forbidden_strings(records: list[dict]) -> list[str]:
    """Строки, которых не должно быть во входе судьи: условие, версия API, разметка дрейфа,
    системные промпты (с changelog'ом), имя эксперимента."""
    from mas_drift.agents.prompts import CHANGELOG_HEADER, SYSTEM_PROMPTS
    from mas_drift.tools.changelogs import CHANGELOGS, IRRELEVANT_CHANGELOG
    from mas_drift.tools.versions import API_VERSIONS

    start = records[0]
    cond = start["condition"]
    out = [f"{cond['api_version']}:{cond['awareness']}", start["experiment"], "drift_affected", "drift_type",
           "shadow_v1_result", CHANGELOG_HEADER, *[v for v in API_VERSIONS if v != "v1"]]
    # Системные промпты и changelog'и — построчно: утечкой считается любая непустая строка из них.
    for text in [*SYSTEM_PROMPTS.values(), *CHANGELOGS.values(), IRRELEVANT_CHANGELOG]:
        out += [line.strip() for line in text.splitlines() if len(line.strip()) >= 20]
    for r in records:
        if r.get("type") == "step":
            out.append(r["messages"][0]["content"])
    return list(dict.fromkeys(out))


def leaks(text: str, records: list[dict]) -> list[str]:
    """Найденные во входе запрещённые строки. Имя эксперимента ищется как отдельное слово
    («main» не должно срабатывать на «remaining»)."""
    exp = records[0]["experiment"]
    found = []
    for s in forbidden_strings(records):
        if not s:
            continue
        hit = re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text) if s == exp else s in text
        if hit:
            found.append(s)
    return found
