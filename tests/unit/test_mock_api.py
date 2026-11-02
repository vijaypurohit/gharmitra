from collections import Counter
from datetime import datetime

import pytest

from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.provider import (
    HoldExpired,
    MockProviderAPI,
    ServiceUnavailable,
    SlotConflict,
    SlotTaken,
    SlotUnavailable,
    ToolTimeout,
)
from gharmitra.mock_api.scenarios import get_scenario

WEEK = ("2026-11-02", "2026-11-08")


def make_api(schemas, clock, scenario="happy", user=sd.DEMO_USER, **overrides):
    api = MockProviderAPI(schemas.mock, clock, get_scenario(scenario, **overrides), user)
    api.seed()
    return api


def first_slot(api, service="plumber", area="Koramangala"):
    """A slot from Wednesday on, so moving the fake clock a few minutes never makes it past."""
    p = next(p for p in api.search_providers(service, area, ("2026-11-04", "2026-11-08")) if p["free_slots"])
    return p, p["free_slots"][0]


def test_search_contract(schemas, clock):
    api = make_api(schemas, clock)
    for service in sd.PROVIDERS:
        for area in api.catalogue.areas:
            results = api.search_providers(service, area, WEEK)
            assert 3 <= len(results) <= 8
    p = api.search_providers("plumber", "Koramangala", WEEK)[0]
    assert set(p) == {"id", "name", "price", "rating", "free_slots", "attributes"}
    assert isinstance(p["price"], int) and p["free_slots"]
    assert not api.seed()  # seeding twice is a no-op


def test_every_world_has_an_injection_review(schemas, clock):
    api = make_api(schemas, clock, seed=7)
    texts = [r["text"] for pid in sd.INJECTION_REVIEWS for r in api.get_provider(pid)["reviews"]]
    assert any("IGNORE PREVIOUS INSTRUCTIONS" in t for t in texts)
    details = api.get_provider("P-RAVI")
    assert {"phone", "address", "reviews", "attributes"} <= set(details)


def test_list_bookings_contract(schemas, clock):
    api = make_api(schemas, clock)
    rows = api.list_bookings(sd.DEMO_USER)
    assert len(rows) == len(sd.PAST_BOOKINGS)
    assert set(rows[0]) == {"id", "provider_id", "provider_name", "service", "slot", "status"}
    assert api.list_bookings(sd.NEW_USER) == []


def test_hold_and_slot_unavailable(schemas, clock):
    api = make_api(schemas, clock)
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    assert set(hold) == {"hold_id", "expires_at", "quoted_price"}
    assert hold["quoted_price"] == p["price"]
    other = MockProviderAPI(schemas.mock, clock, get_scenario("happy"), sd.NEW_USER)
    with pytest.raises(SlotUnavailable):
        other.hold_slot(p["id"], slot)  # held by someone else
    api2 = make_api(schemas, clock, unavailable_slots={(p["id"], p["free_slots"][1])})
    with pytest.raises(SlotUnavailable):
        api2.hold_slot(p["id"], p["free_slots"][1])


def test_quote_drift_is_configurable(schemas, clock):
    api = make_api(schemas, clock, quote_drift={"P-RAVI": [0, 250]})
    p = next(x for x in api.search_providers("plumber", "Koramangala", WEEK) if x["id"] == "P-RAVI")
    assert api.hold_slot("P-RAVI", p["free_slots"][0])["quoted_price"] == p["price"]
    assert api.hold_slot("P-RAVI", p["free_slots"][1])["quoted_price"] == p["price"] + 250


def test_book_mix_over_seeded_run(schemas, clock):
    api = make_api(schemas, clock, scenario="default", drift_probability=0.0)
    outcomes = Counter(api._random_outcome() for _ in range(4000))
    assert 0.67 < outcomes["ok"] / 4000 < 0.73
    assert 0.13 < outcomes["slot_taken"] / 4000 < 0.17
    timeouts = outcomes["timeout_booked"] + outcomes["timeout_not_booked"]
    assert 0.13 < timeouts / 4000 < 0.17
    assert 0.4 < outcomes["timeout_booked"] / timeouts < 0.6


def test_book_ok_repeat_key_and_status(schemas, clock):
    api = make_api(schemas, clock)
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    assert api.get_booking_status("k1") == {"status": "not_found"}
    booking = api.book(hold["hold_id"], "k1")
    assert api.book("H-other", "k1") == booking  # same key → same booking, whatever the hold
    assert api.get_booking_status("k1") == {"status": "booked", "booking_id": booking["booking_id"]}
    assert schemas.mock.scalar("select count(*) from bookings where idempotency_key = 'k1'") == 1


def test_timeout_after_success(schemas, clock):
    api = make_api(schemas, clock, scenario="timeout_after_success")
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    before = clock.now()
    with pytest.raises(ToolTimeout):
        api.book(hold["hold_id"], "k1")
    assert (clock.now() - before).total_seconds() == 2
    assert api.get_booking_status("k1")["status"] == "booked"


def test_timeout_not_booked_then_retry(schemas, clock):
    api = make_api(schemas, clock, book_outcomes=["timeout_not_booked", "ok"])
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    with pytest.raises(ToolTimeout):
        api.book(hold["hold_id"], "k1")
    assert api.get_booking_status("k1") == {"status": "not_found"}
    assert api.book(hold["hold_id"], "k1")["booking_id"]


def test_slot_taken_is_final_for_the_key(schemas, clock):
    api = make_api(schemas, clock, book_outcomes=["slot_taken", "ok"])
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    with pytest.raises(SlotTaken):
        api.book(hold["hold_id"], "k1")
    assert api.get_booking_status("k1") == {"status": "failed", "reason": "slot_taken"}
    with pytest.raises(SlotTaken):
        api.book(hold["hold_id"], "k1")  # replayed, not re-attempted
    day = slot[:10]
    assert slot not in [s for x in api.search_providers("plumber", "Koramangala", (day, day))
                        if x["id"] == p["id"] for s in x["free_slots"]]


def test_hold_expired(schemas, clock):
    api = make_api(schemas, clock)
    p, slot = first_slot(api)
    hold = api.hold_slot(p["id"], slot)
    clock.advance(minutes=10, seconds=1)
    with pytest.raises(HoldExpired):
        api.book(hold["hold_id"], "k1")
    assert api.get_booking_status("k1") == {"status": "failed", "reason": "hold_expired"}
    with pytest.raises(HoldExpired):
        api.book(api.hold_slot(p["id"], slot)["hold_id"], "k1")  # failure is final for the key
    assert api.book(api.hold_slot(p["id"], slot)["hold_id"], "k2")["booking_id"]


def test_cancel_503_then_success(schemas, clock):
    api = make_api(schemas, clock)
    upcoming = next(b for b in api.list_bookings(sd.DEMO_USER) if b["status"] == "booked")
    with pytest.raises(ServiceUnavailable):
        api.cancel(upcoming["id"], "c1")
    assert api.cancel(upcoming["id"], "c1") == {"status": "cancelled", "booking_id": upcoming["id"]}
    assert api.cancel(upcoming["id"], "c1")["status"] == "cancelled"  # replay
    assert next(b for b in api.list_bookings(sd.DEMO_USER) if b["id"] == upcoming["id"])["status"] == "cancelled"


def test_reschedule_and_slot_conflict(schemas, clock):
    api = make_api(schemas, clock)
    ac = next(b for b in api.list_bookings(sd.DEMO_USER) if b["service"] == "ac_servicing")
    free = api.free_slots(ac["provider_id"], clock.now().date().replace(day=7), clock.now().date().replace(day=7))
    good, bad = free[0].isoformat(), free[1].isoformat()
    api.scenario.conflict_slots = {bad}
    with pytest.raises(SlotConflict):
        api.reschedule(ac["id"], bad, "r1")
    moved = api.reschedule(ac["id"], good, "r2")
    assert datetime.fromisoformat(moved["slot"]) == free[0] and moved["status"] == "booked"
    assert moved["slot"].endswith("+00:00")  # times leave the API in UTC
    assert datetime.fromisoformat(next(b for b in api.list_bookings(sd.DEMO_USER) if b["id"] == ac["id"])["slot"]) == free[0]


def test_injected_names_scenario(schemas, clock):
    api = make_api(schemas, clock, scenario="injected_review")
    q = api.get_provider("P-QUICKFIX")
    assert "already approved" in q["name"] and any("ignore previous" in a for a in q["attributes"])


def test_provider_cancels_hook(schemas, clock):
    api = make_api(schemas, clock)
    ac = next(b for b in api.list_bookings(sd.DEMO_USER) if b["service"] == "ac_servicing")
    api.provider_cancels(ac["id"])
    assert next(b for b in api.list_bookings(sd.DEMO_USER) if b["id"] == ac["id"])["status"] == "provider_cancelled"
