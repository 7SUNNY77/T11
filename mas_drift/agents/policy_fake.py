"""PolicyFake — правиловый «агент» ТОЛЬКО для сквозного теста логики пайплайна.

Ведёт себя как наивная система под v1 (параметр nights, поле price, цены в USD). Если в системном
промпте есть changelog с соответствующим изменением, «понимает» его. Результаты fake-провайдера
пишутся в отдельный эксперимент (runs/<fake_name>/) и не смешиваются с реальными.
"""

from __future__ import annotations

import json
import re

from mas_drift.agents import prompts
from mas_drift.agents.jsonutil import extract_json
from mas_drift.infra.fake import fake_usage, text, tool_calls
from mas_drift.infra.llm import LLMClient, LLMResponse

_REQUEST = re.compile(r"из (\S+) в (\S+) на (\d{4}-\d{2}-\d{2}), (\d+) \S+, бюджет (\d+(?:\.\d+)?) USD")


def _parse_request(request: str) -> dict:
    m = _REQUEST.search(request)
    if not m:
        return {}
    origin, city, day, nights, budget = m.groups()
    return {"origin": origin, "destination": city, "date": day, "nights": int(nights), "budget_usd": float(budget)}


class PolicyFake(LLMClient):
    def __init__(self, model: str = "policy-fake"):
        self.provider, self.model, self.temperature = "fake", model, 0.0

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        system = messages[0]["content"]
        if system.startswith(prompts.PLANNER_ROLE):
            r = self._planner(messages)
        elif system.startswith(prompts.EXECUTOR_ROLE):
            r = self._executor(system, messages)
        elif system.startswith(prompts.VERIFIER_ROLE):
            r = self._verifier(messages)
        else:
            raise ValueError("PolicyFake: неизвестная роль")
        r.model = self.model
        r.usage = fake_usage(messages, tools, r)
        return r

    def _planner(self, messages: list[dict]) -> LLMResponse:
        plan = _parse_request(messages[1]["content"])
        plan |= {"goal": "cheapest hotel + flight within budget", "steps": ["search hotels", "search flights"]}
        return text(json.dumps(plan, ensure_ascii=False))

    def _executor(self, system: str, messages: list[dict]) -> LLMResponse:
        knows_contract = "`num_nights`" in system
        knows_units = "in cents" in system
        plan = extract_json(prompts.split_sections(messages[1]["content"]).get(prompts.PLAN_HDR)) or {}

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            nights_key = "num_nights" if knows_contract else "nights"
            return tool_calls(
                ("search_hotels", {"city": plan.get("destination"), nights_key: plan.get("nights")}),
                ("search_flights", {"origin": plan.get("origin"), "destination": plan.get("destination"),
                                    "date": plan.get("date")}),
            )

        names = {tc["id"]: tc["function"]["name"]
                 for m in messages if m["role"] == "assistant" for tc in m.get("tool_calls", [])}
        price_key = "price_total" if knows_contract else "price"
        found: dict[str, list[dict]] = {"search_hotels": [], "search_flights": []}
        for m in tool_msgs:
            data = json.loads(m["content"])
            if not isinstance(data, list):
                continue  # ошибка инструмента: наивный агент не восстанавливается
            for item in data:
                if price_key in item:
                    price = item[price_key] / 100 if knows_units else item[price_key]
                    found[names[m["tool_call_id"]]].append({"id": item["id"], "price_usd": price})
        hotels, flights = found["search_hotels"][:3], found["search_flights"][:3]
        best = None
        if hotels and flights:
            best = {"hotel_id": hotels[0]["id"], "flight_id": flights[0]["id"],
                    "total_usd": round(hotels[0]["price_usd"] + flights[0]["price_usd"], 2)}
        return text(json.dumps({"hotels": hotels, "flights": flights, "best": best, "notes": ""}))

    def _verifier(self, messages: list[dict]) -> LLMResponse:
        sections = prompts.split_sections(messages[1]["content"])
        budget = _parse_request(sections.get(prompts.REQUEST_HDR, "")).get("budget_usd")
        report = extract_json(sections.get(prompts.REPORT_HDR)) or {}
        best = report.get("best") if isinstance(report, dict) else None
        if best and budget is not None and best["total_usd"] <= budget:
            return text(json.dumps(best))
        return text(json.dumps({"no_option": True}))
