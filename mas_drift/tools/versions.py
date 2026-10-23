"""Mock-API командировок в четырёх версиях.

v1          — базовая.
v2_contract — параметр nights → num_nights, выходное поле price → price_total (в обоих инструментах).
v2_units    — схема и описания как в v1, но цены в центах (целые числа, ×100).
v2_benign   — у search_hotels добавлены необязательные поля rating, amenities и параметр
              sort_by (по умолчанию "price"); поведение по умолчанию как в v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mas_drift.domain.world import World, to_cents
from mas_drift.tools import errors

API_VERSIONS = ["v1", "v2_contract", "v2_units", "v2_benign"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HotelsArgsV1(_Args):
    city: str
    nights: int = Field(ge=1)
    max_results: int = Field(10, ge=1, le=50)


class HotelsArgsContract(_Args):
    city: str
    num_nights: int = Field(ge=1)
    max_results: int = Field(10, ge=1, le=50)


class HotelsArgsBenign(HotelsArgsV1):
    sort_by: Literal["price", "rating"] = "price"


class FlightsArgs(_Args):
    origin: str
    destination: str
    date: str


@dataclass(frozen=True)
class VersionSpec:
    name: str
    hotel_args: type[_Args]
    nights_param: str
    price_field: str
    cents: bool
    extra_hotel_fields: bool
    schemas: list[dict] = field(repr=False)


# --- JSON-схемы для function calling. Описание цены намеренно нейтральное, без валюты. ---

_CITY = {"type": "string", "description": "City name, e.g. 'Lisbon'."}
_MAX_RESULTS = {
    "type": "integer",
    "description": "Maximum number of results to return (default 10).",
    "minimum": 1,
    "maximum": 50,
}


def _hotels_schema(nights_param: str, price_field: str, benign: bool) -> dict:
    props: dict[str, Any] = {
        "city": _CITY,
        nights_param: {"type": "integer", "description": "Number of nights of the stay.", "minimum": 1},
        "max_results": _MAX_RESULTS,
    }
    item = f"{{id, name, city, {price_field}}}"
    desc = (
        "Search hotels in a city for a stay of the given number of nights. "
        f"Returns a list of hotels sorted by {price_field} ascending. Each item: {item}, "
        f"where {price_field} is the price of the whole stay."
    )
    if benign:
        props["sort_by"] = {
            "type": "string",
            "enum": ["price", "rating"],
            "description": "Sort order: 'price' (default, ascending) or 'rating' (descending).",
        }
        desc = (
            "Search hotels in a city for a stay of the given number of nights. "
            f"Returns a list of hotels sorted by {price_field} ascending (see sort_by). Each item: "
            f"{{id, name, city, {price_field}, rating?, amenities?}}, where {price_field} is the price "
            "of the whole stay; rating (0-5) and amenities (list of strings) are optional."
        )
    return {
        "type": "function",
        "function": {
            "name": "search_hotels",
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": ["city", nights_param],
            },
        },
    }


def _flights_schema(price_field: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": (
                "Search flights from origin to destination on a given date. "
                f"Returns a list of flights sorted by {price_field} ascending. Each item: "
                f"{{id, origin, destination, date, {price_field}}}, where {price_field} is the ticket price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure city name."},
                    "destination": {"type": "string", "description": "Arrival city name."},
                    "date": {"type": "string", "description": "Departure date, YYYY-MM-DD."},
                },
                "required": ["origin", "destination", "date"],
            },
        },
    }


_V1_SCHEMAS = [_hotels_schema("nights", "price", benign=False), _flights_schema("price")]

SPECS: dict[str, VersionSpec] = {
    "v1": VersionSpec("v1", HotelsArgsV1, "nights", "price", False, False, _V1_SCHEMAS),
    "v2_contract": VersionSpec(
        "v2_contract", HotelsArgsContract, "num_nights", "price_total", False, False,
        [_hotels_schema("num_nights", "price_total", benign=False), _flights_schema("price_total")],
    ),
    "v2_units": VersionSpec("v2_units", HotelsArgsV1, "nights", "price", True, False, _V1_SCHEMAS),
    "v2_benign": VersionSpec(
        "v2_benign", HotelsArgsBenign, "nights", "price", False, True,
        [_hotels_schema("nights", "price", benign=True), _flights_schema("price")],
    ),
}


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


class ToolSet:
    """Набор инструментов одной версии API поверх мира. Вызовы чистые, без побочных эффектов."""

    def __init__(self, world: World, spec: VersionSpec):
        self.world = world
        self.spec = spec
        self.version = spec.name

    @property
    def tool_names(self) -> list[str]:
        return [s["function"]["name"] for s in self.spec.schemas]

    def schemas(self) -> list[dict]:
        return self.spec.schemas

    def _price(self, cents: int) -> float | int:
        return cents if self.spec.cents else cents / 100

    def call(self, name: str, args: dict) -> Any:
        if name == "search_hotels":
            return self._call(self.spec.hotel_args, args, self._search_hotels)
        if name == "search_flights":
            return self._call(FlightsArgs, args, self._search_flights)
        return errors.unknown_tool_error(name, self.tool_names)

    @staticmethod
    def _call(model: type[_Args], args: dict, fn) -> Any:
        try:
            parsed = model.model_validate(args)
        except ValidationError as exc:
            return errors.validation_error(exc)
        return fn(parsed)

    def _search_hotels(self, a: _Args) -> list[dict]:
        nights = getattr(a, self.spec.nights_param)
        rows = []
        for h in self.world.hotels:
            if not _same(h.city, a.city):
                continue
            cents = to_cents(h.price_per_night) * nights
            row = {"id": h.id, "name": h.name, "city": h.city, self.spec.price_field: self._price(cents)}
            if self.spec.extra_hotel_fields:
                row["rating"] = h.rating
                row["amenities"] = list(h.amenities)
            rows.append((cents, h, row))
        if getattr(a, "sort_by", "price") == "rating":
            rows.sort(key=lambda r: (-r[1].rating, r[0], r[1].id))
        else:
            rows.sort(key=lambda r: (r[0], r[1].id))
        return [r[2] for r in rows[: a.max_results]]

    def _search_flights(self, a: FlightsArgs) -> list[dict]:
        rows = [
            (to_cents(f.price), f.id, {
                "id": f.id, "origin": f.origin, "destination": f.destination, "date": f.date,
                self.spec.price_field: self._price(to_cents(f.price)),
            })
            for f in self.world.flights
            if _same(f.origin, a.origin) and _same(f.destination, a.destination) and f.date == a.date.strip()
        ]
        rows.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in rows]


def get_toolset(version: str, world: World) -> ToolSet:
    if version not in SPECS:
        raise KeyError(f"Неизвестная версия API {version!r}; есть {API_VERSIONS}")
    return ToolSet(world, SPECS[version])
