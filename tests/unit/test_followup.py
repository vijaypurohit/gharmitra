"""Provider-cancel follow-up: the event is checked with the provider before anything is recorded."""

from gharmitra.agent.followup import provider_cancelled
from tests.evals.harness import make_world


def test_event_not_confirmed_by_provider_changes_nothing(schemas):
    ev = make_world(schemas, "provider_cancels")
    reply = provider_cancelled(ev.controller, "BK-905")  # the agent hears of a cancel the provider never made
    assert reply is not None and reply.phase == "COLLECTING"
    assert [e["payload"]["booking_id"] for e in ev.events("followup_ignored")] == ["BK-905"]
    assert ev.db.scalar("select status from bookings where id = 'BK-905'") == "booked"
    assert ev.db.scalar("select count(*) from refunds") == 0
    assert not ev.tool_calls("search_providers")


def test_unknown_or_finished_booking_is_ignored(schemas):
    ev = make_world(schemas, "provider_cancels")
    assert ev.world.provider_cancels("BK-NOPE") is None
    assert provider_cancelled(ev.controller, "BK-901") is None  # completed, not booked
