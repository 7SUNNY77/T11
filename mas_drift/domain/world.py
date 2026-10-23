"""Mock-мир командировок: города, отели, рейсы. Полностью детерминирован seed'ом.

Цены хранятся в USD с двумя знаками; любые сравнения и суммы делаются в целых центах
(`to_cents`), чтобы ничьи определялись точно, без погрешности float.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import date, timedelta

from pydantic import BaseModel

CITY_NAMES = [
    "Amsterdam", "Berlin", "Lisbon", "Madrid", "Prague",
    "Rome", "Vienna", "Warsaw", "Budapest", "Copenhagen",
]
HOTEL_PREFIXES = ["Grand", "Royal", "City", "Park", "Central", "Old Town", "River", "Garden", "Star", "Harbor"]
HOTEL_SUFFIXES = ["Hotel", "Inn", "Suites", "Residence", "Lodge", "Palace", "House", "Hostel"]
AMENITIES = ["wifi", "breakfast", "parking", "gym", "pool", "spa", "airport_shuttle"]

START_DATE = date(2026, 11, 2)
N_DAYS = 7

# Доля городов / рейсовых направлений-дат, где намеренно создаётся ничья по минимальной цене.
HOTEL_TIE_RATE = 0.35
FLIGHT_TIE_RATE = 0.25


def to_cents(usd: float) -> int:
    return round(usd * 100)


class Hotel(BaseModel):
    id: str
    name: str
    city: str
    price_per_night: float  # USD
    # Используются только в v2_benign; в мир заложены сразу, чтобы мир не зависел от версии API.
    rating: float
    amenities: list[str]


class Flight(BaseModel):
    id: str
    origin: str
    destination: str
    date: str  # YYYY-MM-DD
    price: float  # USD


class World(BaseModel):
    seed: int
    cities: list[str]
    hotels: list[Hotel]
    flights: list[Flight]

    def hotels_in(self, city: str) -> list[Hotel]:
        return [h for h in self.hotels if h.city == city]

    def flights_on(self, origin: str, destination: str, day: str) -> list[Flight]:
        return [
            f for f in self.flights
            if f.origin == origin and f.destination == destination and f.date == day
        ]

    def fingerprint(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _price(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def generate_world(seed: int) -> World:
    rng = random.Random(seed)
    cities = sorted(rng.sample(CITY_NAMES, rng.randint(6, 8)))

    hotels: list[Hotel] = []
    for city in cities:
        city_hotels = []
        names_used: set[str] = set()
        for _ in range(rng.randint(8, 15)):
            while True:
                name = f"{rng.choice(HOTEL_PREFIXES)} {city} {rng.choice(HOTEL_SUFFIXES)}"
                if name not in names_used:
                    names_used.add(name)
                    break
            city_hotels.append(
                dict(
                    name=name,
                    city=city,
                    price_per_night=_price(rng, 40, 320),
                    rating=round(rng.uniform(2.5, 5.0), 1),
                    amenities=sorted(rng.sample(AMENITIES, rng.randint(1, 4))),
                )
            )
        if rng.random() < HOTEL_TIE_RATE:
            # Ничья: другой отель получает ту же минимальную цену за ночь.
            cheapest = min(city_hotels, key=lambda h: h["price_per_night"])
            other = rng.choice([h for h in city_hotels if h is not cheapest])
            other["price_per_night"] = cheapest["price_per_night"]
        hotels.extend(Hotel(id="", **h) for h in city_hotels)

    # Идентификаторы непрозрачные и сквозные, присваиваются после генерации.
    order = list(range(len(hotels)))
    rng.shuffle(order)
    for new_idx, h_idx in enumerate(order):
        hotels[h_idx].id = f"H{101 + new_idx}"

    flights_raw: list[dict] = []
    for origin in cities:
        for destination in cities:
            if origin == destination:
                continue
            for d in range(N_DAYS):
                day = (START_DATE + timedelta(days=d)).isoformat()
                group = [
                    dict(origin=origin, destination=destination, date=day, price=_price(rng, 60, 480))
                    for _ in range(rng.randint(1, 4))
                ]
                if len(group) >= 2 and rng.random() < FLIGHT_TIE_RATE:
                    cheapest = min(group, key=lambda f: f["price"])
                    other = rng.choice([f for f in group if f is not cheapest])
                    other["price"] = cheapest["price"]
                flights_raw.extend(group)
    rng.shuffle(flights_raw)
    flights = [Flight(id=f"F{1001 + i}", **f) for i, f in enumerate(flights_raw)]

    return World(seed=seed, cities=cities, hotels=hotels, flights=flights)
