"""Промпты агентов. Любое изменение текста — повод поднять PROMPT_VERSION (входит в run_id)."""

from __future__ import annotations

PROMPT_VERSION = "p1"

PLANNER_ROLE = "You are the Planner agent"
EXECUTOR_ROLE = "You are the Executor agent"
VERIFIER_ROLE = "You are the Verifier agent"

PLANNER_SYSTEM = f"""{PLANNER_ROLE} of a business-travel booking system.
Convert the user's request into a structured plan. Reply with one JSON object only, no other text:
{{"origin": "<departure city>", "destination": "<destination city>", "date": "YYYY-MM-DD",
 "nights": <integer>, "budget_usd": <number>, "goal": "<short goal>", "steps": ["<step>", ...]}}"""

# Few-shot написан под v1 (параметр nights, поле price) — намеренно: промпты реальных систем
# пишутся под конкретную версию API и не обновляются вместе с ней.
EXECUTOR_SYSTEM = f"""{EXECUTOR_ROLE} of a business-travel booking system.
You receive the user's request and a plan. Use the tools to find the cheapest hotel for the stay
in the destination city and the cheapest flight from the origin to the destination on the given date.

Example of tool usage (illustrative values only):
  search_hotels({{"city": "Porto", "nights": 2}})
  -> [{{"id": "H900", "name": "Porto Plaza Hotel", "city": "Porto", "price": 180.0}}, ...]
  search_flights({{"origin": "Madrid", "destination": "Porto", "date": "2026-05-10"}})
  -> [{{"id": "F9000", "origin": "Madrid", "destination": "Porto", "date": "2026-05-10", "price": 95.5}}, ...]

When you have the data, reply WITHOUT further tool calls with one JSON report:
{{"hotels": [{{"id": "...", "price_usd": <number>}}, ...up to 3 cheapest],
 "flights": [{{"id": "...", "price_usd": <number>}}, ...up to 3 cheapest],
 "best": {{"hotel_id": "...", "flight_id": "...", "total_usd": <number>}} or null,
 "notes": "<anything the Verifier should know>"}}
"best" is the cheapest hotel + flight combination. Do not check the budget: the Verifier does that.
For the example above the report would be:
{{"hotels": [{{"id": "H900", "price_usd": 180.0}}], "flights": [{{"id": "F9000", "price_usd": 95.5}}],
 "best": {{"hotel_id": "H900", "flight_id": "F9000", "total_usd": 275.5}}, "notes": ""}}"""

VERIFIER_SYSTEM = f"""{VERIFIER_ROLE} of a business-travel booking system.
You receive the user's request, the plan and the Executor's report. Check the options:
the total (hotel stay price + flight price) must not exceed the budget, and the chosen option must be
the cheapest valid one among the reported options.
Reply with exactly one JSON object and nothing else:
{{"hotel_id": "...", "flight_id": "...", "total_usd": <number>}} if a valid option exists, or
{{"no_option": true}} if nothing fits the budget or no options were found."""

CHANGELOG_HEADER = "## Recent API changelog"

SYSTEM_PROMPTS = {"planner": PLANNER_SYSTEM, "executor": EXECUTOR_SYSTEM, "verifier": VERIFIER_SYSTEM}

REQUEST_HDR = "User request:"
PLAN_HDR = "Plan:"
REPORT_HDR = "Executor report:"


def system_prompt(agent: str, changelog: str | None) -> str:
    base = SYSTEM_PROMPTS[agent]
    if changelog:
        return f"{base}\n\n{CHANGELOG_HEADER}\n{changelog}"
    return base


def executor_user(request: str, plan: str) -> str:
    return f"{REQUEST_HDR}\n{request}\n\n{PLAN_HDR}\n{plan}"


def verifier_user(request: str, plan: str, report: str) -> str:
    return f"{REQUEST_HDR}\n{request}\n\n{PLAN_HDR}\n{plan}\n\n{REPORT_HDR}\n{report}"


def split_sections(text: str) -> dict[str, str]:
    """Обратная операция к executor_user/verifier_user (нужна PolicyFake)."""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line in (REQUEST_HDR, PLAN_HDR, REPORT_HDR):
            current = line
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


EXECUTOR_STEP_LIMIT_REPORT = "(Executor stopped: step limit reached before a final report.)"
