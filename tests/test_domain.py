import itertools

import pytest

from mas_drift.domain import Flight, Hotel, World, compute_gold, generate_tasks, generate_world
from mas_drift.domain.tasks import nights_word, tasks_fingerprint

SEED = 42


@pytest.fixture(scope="module")
def world():
    return generate_world(SEED)


@pytest.fixture(scope="module")
def tasks(world):
    return generate_tasks(world, 30, SEED)


def test_world_is_reproducible(world):
    assert generate_world(SEED).fingerprint() == world.fingerprint()
    assert generate_world(SEED + 1).fingerprint() != world.fingerprint()


def test_world_shape(world):
    assert 6 <= len(world.cities) <= 8
    for city in world.cities:
        assert 8 <= len(world.hotels_in(city)) <= 15
    ids = [h.id for h in world.hotels] + [f.id for f in world.flights]
    assert len(ids) == len(set(ids))
    assert all(f.origin != f.destination for f in world.flights)


def test_tasks_reproducible(world, tasks):
    again = generate_tasks(generate_world(SEED), 30, SEED)
    assert tasks_fingerprint(again) == tasks_fingerprint(tasks)
    assert len(tasks) == 30
    assert len({t.task_id for t in tasks}) == 30


def test_task_mix(tasks):
    kinds = [t.designed_as for t in tasks]
    assert kinds.count("no_option") == 5
    assert kinds.count("tie") == 5
    for t in tasks:
        if t.designed_as == "tie":
            assert len(t.gold.pairs) > 1
        if t.designed_as == "plain":
            assert len(t.gold.pairs) == 1
        assert (t.gold.kind == "no_option") == (t.designed_as == "no_option")


def _brute_force(world, t):
    """Независимая реализация эталона через itertools.product и float с допуском."""
    options = [
        (round(h.price_per_night * t.nights + f.price, 2), h.id, f.id)
        for h, f in itertools.product(
            [h for h in world.hotels if h.city == t.city],
            [f for f in world.flights if (f.origin, f.destination, f.date) == (t.origin, t.city, t.date)],
        )
    ]
    ok = [o for o in options if o[0] <= t.budget + 1e-9]
    if not ok:
        return None, set()
    m = min(o[0] for o in ok)
    return m, {(h, f) for total, h, f in ok if abs(total - m) < 1e-6}


def test_gold_matches_brute_force(world, tasks):
    for t in tasks:
        total, pairs = _brute_force(world, t)
        if total is None:
            assert t.gold.kind == "no_option"
        else:
            assert t.gold.kind == "pairs"
            assert t.gold.pair_set() == pairs
            assert t.gold.total_usd == pytest.approx(total, abs=0.005)


def test_budget_relations(world, tasks):
    for t in tasks:
        unlimited = compute_gold(world, t.origin, t.city, t.date, t.nights, 1e9)
        if t.gold.kind == "no_option":
            assert t.budget < unlimited.total_usd
        else:
            assert unlimited.total_usd <= t.budget
            assert unlimited.pair_set() == t.gold.pair_set()


def _toy_world():
    hotels = [
        Hotel(id="H1", name="a", city="X", price_per_night=50.0, rating=4.0, amenities=[]),
        Hotel(id="H2", name="b", city="X", price_per_night=50.0, rating=3.0, amenities=[]),
        Hotel(id="H3", name="c", city="X", price_per_night=70.0, rating=5.0, amenities=[]),
        Hotel(id="H4", name="d", city="Y", price_per_night=10.0, rating=5.0, amenities=[]),
    ]
    flights = [
        Flight(id="F1", origin="Y", destination="X", date="2026-11-02", price=100.10),
        Flight(id="F2", origin="Y", destination="X", date="2026-11-02", price=100.10),
        Flight(id="F3", origin="Y", destination="X", date="2026-11-02", price=120.00),
        Flight(id="F4", origin="Y", destination="X", date="2026-11-03", price=1.00),
    ]
    return World(seed=0, cities=["X", "Y"], hotels=hotels, flights=flights)


def test_gold_ties_cross_product():
    g = compute_gold(_toy_world(), "Y", "X", "2026-11-02", nights=2, budget=500)
    assert g.kind == "pairs"
    assert g.total_usd == pytest.approx(200.10)
    assert g.pair_set() == {("H1", "F1"), ("H1", "F2"), ("H2", "F1"), ("H2", "F2")}


def test_gold_budget_boundary_and_no_option():
    w = _toy_world()
    exact = compute_gold(w, "Y", "X", "2026-11-02", nights=2, budget=200.10)
    assert exact.kind == "pairs"
    below = compute_gold(w, "Y", "X", "2026-11-02", nights=2, budget=200.09)
    assert below.kind == "no_option" and below.pairs == [] and below.total_usd is None
    no_flights = compute_gold(w, "X", "Y", "2026-11-02", nights=1, budget=10_000)
    assert no_flights.kind == "no_option"


def test_nights_word():
    assert [nights_word(n) for n in (1, 2, 5, 11, 21, 22)] == ["ночь", "ночи", "ночей", "ночей", "ночь", "ночи"]


def test_request_text(tasks):
    t = tasks[0]
    assert t.request.startswith(f"Найди самый дешёвый вариант отель + перелёт из {t.origin} в {t.city} на {t.date}")
    assert t.request.endswith(f"бюджет {t.budget} USD")
