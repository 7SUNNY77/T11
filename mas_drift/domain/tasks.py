"""Генератор задач и программный эталон."""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Literal

from pydantic import BaseModel

from mas_drift.domain.world import World, to_cents

NO_OPTION_SHARE = 0.15
TIE_SHARE = 0.15

REQUEST_TEMPLATE = (
    "Найди самый дешёвый вариант отель + перелёт из {origin} в {city} на {date}, "
    "{nights} {nights_word}, бюджет {budget} USD"
)


def nights_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ночь"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ночи"
    return "ночей"


class Gold(BaseModel):
    kind: Literal["pairs", "no_option"]
    pairs: list[tuple[str, str]] = []  # все пары (hotel_id, flight_id) с минимальной суммой
    total_usd: float | None = None

    def pair_set(self) -> frozenset[tuple[str, str]]:
        return frozenset(tuple(p) for p in self.pairs)


class Task(BaseModel):
    task_id: str
    origin: str
    city: str
    date: str
    nights: int
    budget: int  # USD, целое
    request: str
    gold: Gold
    designed_as: Literal["plain", "tie", "no_option"]


def compute_gold(world: World, origin: str, city: str, day: str, nights: int, budget: float) -> Gold:
    """Перебор всех пар отель × рейс в рамках бюджета; возвращает все пары с минимальной суммой."""
    budget_c = to_cents(budget)
    best_c: int | None = None
    best: list[tuple[str, str]] = []
    for h in world.hotels_in(city):
        stay_c = to_cents(h.price_per_night) * nights
        for f in world.flights_on(origin, city, day):
            total_c = stay_c + to_cents(f.price)
            if total_c > budget_c:
                continue
            if best_c is None or total_c < best_c:
                best_c, best = total_c, [(h.id, f.id)]
            elif total_c == best_c:
                best.append((h.id, f.id))
    if best_c is None:
        return Gold(kind="no_option")
    return Gold(kind="pairs", pairs=sorted(best), total_usd=best_c / 100)


def _min_total_cents(world: World, origin: str, city: str, day: str, nights: int) -> int:
    stay = min(to_cents(h.price_per_night) for h in world.hotels_in(city)) * nights
    flight = min(to_cents(f.price) for f in world.flights_on(origin, city, day))
    return stay + flight


def _has_tie(world: World, origin: str, city: str, day: str, nights: int) -> bool:
    return len(compute_gold(world, origin, city, day, nights, budget=1e9).pairs) > 1


def generate_tasks(world: World, n: int, seed: int) -> list[Task]:
    rng = random.Random(seed * 1_000_003 + 17)
    n_no = math.ceil(n * NO_OPTION_SHARE)
    n_tie = math.ceil(n * TIE_SHARE)
    kinds = ["no_option"] * n_no + ["tie"] * n_tie + ["plain"] * (n - n_no - n_tie)
    rng.shuffle(kinds)

    routes = sorted(
        {(f.origin, f.destination, f.date) for f in world.flights}
    )
    tasks: list[Task] = []
    seen: set[tuple[str, str, str, int]] = set()
    for i, kind in enumerate(kinds):
        for _ in range(10_000):
            origin, city, day = rng.choice(routes)
            nights = rng.randint(1, 7)
            key = (origin, city, day, nights)
            if key in seen:
                continue
            tie = _has_tie(world, origin, city, day, nights)
            if kind == "tie" and not tie:
                continue
            if kind != "tie" and tie:
                # Ничьи — только в задачах, где они запланированы, чтобы их доля была контролируемой.
                continue
            break
        else:
            raise RuntimeError(f"Не удалось подобрать задачу вида {kind}")
        seen.add(key)

        min_c = _min_total_cents(world, origin, city, day, nights)
        if kind == "no_option":
            # Бюджет чуть ниже минимума: «почти хватает».
            budget = math.floor(min_c / 100 * rng.uniform(0.85, 0.98))
        else:
            budget = math.ceil(min_c / 100 * rng.uniform(1.05, 1.40))

        gold = compute_gold(world, origin, city, day, nights, budget)
        assert (gold.kind == "no_option") == (kind == "no_option")
        tasks.append(
            Task(
                task_id=f"T{i + 1:02d}",
                origin=origin,
                city=city,
                date=day,
                nights=nights,
                budget=budget,
                request=REQUEST_TEMPLATE.format(
                    origin=origin, city=city, date=day, nights=nights,
                    nights_word=nights_word(nights), budget=budget,
                ),
                gold=gold,
                designed_as=kind,
            )
        )
    return tasks


def tasks_fingerprint(tasks: list[Task]) -> str:
    blob = json.dumps([t.model_dump() for t in tasks], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
