import pytest

from mas_drift.domain import generate_tasks, generate_world
from mas_drift.domain.world import to_cents
from mas_drift.tools import API_VERSIONS, DriftObservedToolSet, changelog_for, get_toolset
from mas_drift.tools.changelogs import IRRELEVANT_CHANGELOG
from mas_drift.tools.drift import classify
from mas_drift.tools.errors import is_error
from mas_drift.tools.versions import SPECS


@pytest.fixture(scope="module")
def world():
    return generate_world(42)


@pytest.fixture(scope="module")
def task(world):
    return generate_tasks(world, 30, 42)[0]


def _hotels(world, version, **args):
    return get_toolset(version, world).call("search_hotels", args)


def _flights(world, version, t):
    return get_toolset(version, world).call(
        "search_flights", {"origin": t.origin, "destination": t.city, "date": t.date}
    )


# --- v1 ---

def test_v1_hotels_sorted_whole_stay_price(world, task):
    res = _hotels(world, "v1", city=task.city, nights=3)
    assert len(res) == min(10, len(world.hotels_in(task.city)))
    prices = [r["price"] for r in res]
    assert prices == sorted(prices)
    by_id = {h.id: h for h in world.hotels}
    for r in res:
        assert set(r) == {"id", "name", "city", "price"}
        assert to_cents(r["price"]) == to_cents(by_id[r["id"]].price_per_night) * 3
    cheapest = min(world.hotels_in(task.city), key=lambda h: (to_cents(h.price_per_night), h.id))
    assert res[0]["id"] == cheapest.id


def test_v1_max_results_and_case_insensitive_city(world, task):
    res = _hotels(world, "v1", city=task.city.upper(), nights=1, max_results=3)
    assert len(res) == 3
    assert _hotels(world, "v1", city="Atlantis", nights=1) == []


def test_v1_flights_sorted(world, task):
    res = _flights(world, "v1", task)
    assert res and [r["price"] for r in res] == sorted(r["price"] for r in res)
    assert all(set(r) == {"id", "origin", "destination", "date", "price"} for r in res)
    assert {r["id"] for r in res} == {f.id for f in world.flights_on(task.origin, task.city, task.date)}


def test_v1_lax_int_coercion_and_validation(world, task):
    assert _hotels(world, "v1", city=task.city, nights="2") == _hotels(world, "v1", city=task.city, nights=2)
    err = _hotels(world, "v1", city=task.city, nights=0)
    assert is_error(err) and err["status"] == 422


def test_unknown_tool(world):
    err = get_toolset("v1", world).call("car_rental", {})
    assert is_error(err) and err["status"] == 404


# --- v2_contract ---

def test_contract_rejects_old_param_with_detailed_422(world, task):
    err = _hotels(world, "v2_contract", city=task.city, nights=2)
    assert is_error(err) and err["status"] == 422
    by_loc = {tuple(d["loc"]): d["type"] for d in err["detail"]}
    assert by_loc == {("body", "nights"): "extra_forbidden", ("body", "num_nights"): "missing"}


def test_contract_renames_price_in_both_tools(world, task):
    v1 = _hotels(world, "v1", city=task.city, nights=2)
    v2 = _hotels(world, "v2_contract", city=task.city, num_nights=2)
    assert [r["id"] for r in v1] == [r["id"] for r in v2]
    assert all("price" not in r and "price_total" in r for r in v2)
    assert [r["price"] for r in v1] == [r["price_total"] for r in v2]
    fl = _flights(world, "v2_contract", task)
    assert all("price" not in r and "price_total" in r for r in fl)


def test_contract_schema_updated():
    hotels = SPECS["v2_contract"].schemas[0]["function"]
    assert "num_nights" in hotels["parameters"]["properties"]
    assert "nights" not in hotels["parameters"]["properties"]
    assert hotels["parameters"]["required"] == ["city", "num_nights"]
    assert "price_total" in hotels["description"]


# --- v2_units ---

def test_units_schema_identical_to_v1():
    assert SPECS["v2_units"].schemas == SPECS["v1"].schemas


def test_price_description_is_currency_neutral():
    for version in API_VERSIONS:
        text = str(SPECS[version].schemas).lower()
        for word in ("usd", "dollar", "cent", "currency", "$"):
            assert word not in text, (version, word)


def test_units_prices_in_cents(world, task):
    v1 = _hotels(world, "v1", city=task.city, nights=4)
    v2 = _hotels(world, "v2_units", city=task.city, nights=4)
    assert [r["id"] for r in v1] == [r["id"] for r in v2]
    for a, b in zip(v1, v2):
        assert isinstance(b["price"], int) and b["price"] == to_cents(a["price"])
    for a, b in zip(_flights(world, "v1", task), _flights(world, "v2_units", task)):
        assert b["price"] == to_cents(a["price"])


# --- v2_benign ---

def test_benign_default_matches_v1_plus_optional_fields(world, task):
    v1 = _hotels(world, "v1", city=task.city, nights=2)
    v2 = _hotels(world, "v2_benign", city=task.city, nights=2)
    assert [{k: r[k] for k in ("id", "name", "city", "price")} for r in v2] == v1
    assert all({"rating", "amenities"} <= set(r) for r in v2)
    assert _flights(world, "v2_benign", task) == _flights(world, "v1", task)


def test_benign_sort_by(world, task):
    res = _hotels(world, "v2_benign", city=task.city, nights=2, sort_by="rating")
    assert [r["rating"] for r in res] == sorted((r["rating"] for r in res), reverse=True)
    err = _hotels(world, "v2_benign", city=task.city, nights=2, sort_by="stars")
    assert is_error(err) and err["status"] == 422


def test_schemas_match_arg_models():
    for spec in SPECS.values():
        if spec.name == "v2_contract_stale":
            continue  # расхождение схемы и валидации — суть этого условия
        props = spec.schemas[0]["function"]["parameters"]["properties"]
        assert set(props) == set(spec.hotel_args.model_fields), spec.name


# --- v2_contract_stale ---

def test_stale_agent_sees_v1_schema():
    assert SPECS["v2_contract_stale"].schemas == SPECS["v1"].schemas
    props = SPECS["v2_contract_stale"].schemas[0]["function"]["parameters"]["properties"]
    assert "nights" in props and "num_nights" not in props
    assert get_toolset("v2_contract_stale", generate_world(42)).schemas() == SPECS["v1"].schemas


def test_stale_api_validates_as_v2(world, task):
    err = _hotels(world, "v2_contract_stale", city=task.city, nights=2)
    assert is_error(err) and err["status"] == 422
    assert err == _hotels(world, "v2_contract", city=task.city, nights=2)
    by_loc = {tuple(d["loc"]): d["type"] for d in err["detail"]}
    assert by_loc == {("body", "nights"): "extra_forbidden", ("body", "num_nights"): "missing"}
    ok = _hotels(world, "v2_contract_stale", city=task.city, num_nights=2)
    assert ok == _hotels(world, "v2_contract", city=task.city, num_nights=2)
    assert all("price_total" in r and "price" not in r for r in ok)
    assert _flights(world, "v2_contract_stale", task) == _flights(world, "v2_contract", task)


def test_stale_uses_contract_changelog():
    assert changelog_for("v2_contract_stale") == changelog_for("v2_contract")


def test_stale_drift_annotation(world, task):
    a = DriftObservedToolSet.for_version("v2_contract_stale", world).call(
        "search_hotels", {"city": task.city, "nights": 2})
    assert a.drift_type == "input_rejected" and a.drift_affected


# --- changelogs ---

def test_changelogs():
    assert changelog_for("v1") == IRRELEVANT_CHANGELOG
    assert "car_rental" in IRRELEVANT_CHANGELOG.lower().replace(" ", "_")
    assert "num_nights" in changelog_for("v2_contract") and "price_total" in changelog_for("v2_contract")
    assert "cents" in changelog_for("v2_units")
    assert "sort_by" in changelog_for("v2_benign")
    for v in API_VERSIONS[1:]:
        assert "search_hotels" in changelog_for(v)
    assert "search_hotels" not in IRRELEVANT_CHANGELOG


# --- drift annotation ---

def _annot(world, version, name, args):
    return DriftObservedToolSet.for_version(version, world).call(name, args)


def test_drift_none_on_v1(world, task):
    a = _annot(world, "v1", "search_hotels", {"city": task.city, "nights": 2})
    assert not a.drift_affected and a.drift_type == "none" and a.result == a.shadow_v1_result


@pytest.mark.parametrize(
    "version,name,args,expected",
    [
        ("v2_contract", "search_hotels", {"nights": 2}, "input_rejected"),
        ("v2_contract", "search_hotels", {"num_nights": 2}, "input_v2_only"),
        ("v2_contract", "search_flights", {}, "output_renamed"),
        ("v2_units", "search_hotels", {"nights": 2}, "output_units"),
        ("v2_units", "search_flights", {}, "output_units"),
        ("v2_benign", "search_hotels", {"nights": 2}, "output_added_fields"),
        ("v2_benign", "search_hotels", {"nights": 2, "sort_by": "price"}, "input_v2_only"),
        ("v2_benign", "search_flights", {}, "none"),
    ],
)
def test_drift_types(world, task, version, name, args, expected):
    base = (
        {"city": task.city}
        if name == "search_hotels"
        else {"origin": task.origin, "destination": task.city, "date": task.date}
    )
    a = _annot(world, version, name, {**base, **args})
    assert a.drift_type == expected
    assert a.drift_affected == (expected != "none")


def test_drift_errors_and_empty(world):
    a = _annot(world, "v2_units", "search_hotels", {"city": "Atlantis", "nights": 2})
    assert a.result == [] and a.drift_type == "none"  # пустой ответ не затронут дрейфом
    b = _annot(world, "v2_units", "search_hotels", {"city": "X"})
    assert is_error(b.result) and b.drift_type == "none"


def test_classify_values_changed():
    assert classify([{"id": "a", "p": 1}], [{"id": "a", "p": 2}]) == "output_values_changed"
    assert classify([{"id": "b"}], [{"id": "a"}]) == "results_changed"
