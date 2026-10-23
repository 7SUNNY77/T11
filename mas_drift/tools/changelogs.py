"""Changelog'и версий API на естественном языке (то, что получают агенты в условии A1)."""

from __future__ import annotations

CHANGELOGS: dict[str, str] = {
    "v2_contract": (
        "Travel API v2.0 — BREAKING CHANGES\n"
        "- search_hotels: input parameter `nights` has been renamed to `num_nights`. "
        "Calls that pass `nights` are rejected with a validation error.\n"
        "- search_hotels and search_flights: output field `price` has been renamed to `price_total`. "
        "Values and their meaning are unchanged.\n"
        "- No other changes."
    ),
    "v2_units": (
        "Travel API v2.0 — BREAKING CHANGE\n"
        "- search_hotels and search_flights: all price fields are now returned in cents "
        "(integer, US dollars × 100) instead of US dollars. For example, 412.50 USD is now returned as 41250.\n"
        "- Field names, parameters and sorting are unchanged."
    ),
    "v2_benign": (
        "Travel API v1.1 — backward-compatible changes\n"
        "- search_hotels: results now include optional fields `rating` (0–5) and `amenities` (list of strings).\n"
        "- search_hotels: new optional parameter `sort_by` ('price' | 'rating'), default 'price'. "
        "Default behaviour is unchanged.\n"
        "- search_flights: no changes."
    ),
}

# Контрольное условие для v1 + A1: изменение в инструменте, которого у нас нет.
IRRELEVANT_CHANGELOG = (
    "Car Rental API v3.0 — BREAKING CHANGES\n"
    "- car_rental.search_cars: input parameter `pickup_location` has been renamed to `pickup_city`.\n"
    "- car_rental.search_cars: output field `daily_rate` has been renamed to `rate_per_day`.\n"
    "- No other services are affected."
)


def changelog_for(version: str) -> str:
    if version == "v1":
        return IRRELEVANT_CHANGELOG
    return CHANGELOGS[version]
