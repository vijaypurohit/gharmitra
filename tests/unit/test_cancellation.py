"""Cancellation policy, receipts and the run report (tier 2, step 14)."""

from datetime import datetime, timedelta

import pytest

from gharmitra import receipts
from gharmitra.agent.cancellation import Policy, PolicyStore, can_reschedule, quote
from gharmitra.clock import IST, FakeClock
from gharmitra.money import rupees
from gharmitra.report import build_report, load_events
from tests.evals.harness import NO_PREF, YES, book, days, make_world, pick

SLOT = datetime(2026, 11, 5, 15, 0, tzinfo=IST)
PRICE = rupees(1800)
CANCEL_CLEANING = {"act": "request", "goal": "cancel", "target": {"service": "cleaning"}, "confidence": 0.9}


@pytest.mark.parametrize("before, tier, fee", [
    (timedelta(hours=30), "free", 0),
    (timedelta(hours=24), "free", 0),  # exactly 24 h: still free
    (timedelta(hours=23, minutes=59, seconds=59), "late", rupees(100)),
    (timedelta(hours=3), "late", rupees(100)),  # exactly 3 h: flat fee
    (timedelta(hours=2, minutes=59, seconds=59), "very_late", PRICE // 2),
    (timedelta(seconds=1), "very_late", PRICE // 2),
    (timedelta(0), "started", PRICE),
])
def test_fee_tiers_and_boundaries(before, tier, fee):
    q = quote(Policy(), PRICE, SLOT, SLOT - before)
    assert (q.tier, q.fee) == (tier, fee)
    assert q.refund == (0 if tier == "started" else PRICE - fee)


def test_flat_fee_never_exceeds_price():
    assert quote(Policy(), rupees(60), SLOT, SLOT - timedelta(hours=5)).fee == rupees(60)


def test_reschedule_cut_off():
    assert can_reschedule(Policy(), SLOT, SLOT - timedelta(hours=3))
    assert not can_reschedule(Policy(), SLOT, SLOT - timedelta(hours=2, minutes=59))


def test_policy_validation():
    with pytest.raises(ValueError):
        Policy(late_hours=30).validate()
    with pytest.raises(ValueError):
        Policy(very_late_percent=120).validate()


def test_provider_override_beats_platform(schemas):
    store = PolicyStore(schemas.agent, FakeClock())
    store.set_platform(Policy(late_fee=rupees(150)))
    store.set_override("C-SPARKLE", {"free_hours": 48})
    sparkle, other = store.for_provider("C-SPARKLE"), store.for_provider("P-RAVI")
    assert (sparkle.free_hours, sparkle.late_fee) == (48, rupees(150))
    assert (other.free_hours, other.late_fee) == (24, rupees(150))
    store.set_override("C-SPARKLE", None)
    assert store.for_provider("C-SPARKLE").free_hours == 24


def test_snapshot_unaffected_by_later_edits(schemas):
    ev = make_world(schemas)
    PolicyStore(ev.db, ev.clock).set_platform(Policy(late_fee=rupees(500)))  # after BK-905 was booked
    ev.clock.advance(hours=10)  # 20 h before the slot
    ev.new_session()
    ev.say("Cancel my cleaning booking", CANCEL_CLEANING)
    assert ev.approvals()[0]["amount"] == rupees(1700)  # the ₹100 fee in force at booking time


def test_new_booking_snapshots_current_policy(schemas):
    ev = make_world(schemas)
    PolicyStore(ev.db, ev.clock).set_platform(Policy(late_fee=rupees(250)))
    ev.new_session()
    ev.say("plumber", book(service="plumber", area="Koramangala", date=days("2026-11-04")))
    ev.say("no preference", NO_PREF)
    ev.say("1", pick(1))
    ev.say("yes", YES)
    policy = ev.db.scalar("select policy from bookings where op_key is not null")
    assert policy["late_fee"] == rupees(250) and policy["provider_id"].startswith("P-")


def test_fee_change_before_confirm_voids_and_reasks(schemas):
    ev = make_world(schemas)
    ev.clock.advance(hours=5, minutes=59)  # 24 h 01 m before the slot
    ev.new_session()
    ev.say("Cancel my cleaning booking", CANCEL_CLEANING)
    ev.clock.advance(minutes=2)  # the user answers 2 minutes later, inside the ₹100 tier
    reply = ev.say("yes", YES)
    a = ev.approvals()
    assert [x["status"] for x in a] == ["voided", "pending"] and a[0]["status_reason"] == "values_changed"
    assert a[1]["amount"] == rupees(1700) and "₹100" in a[1]["summary"]
    assert not ev.tool_calls("cancel") and reply.approval["approval_id"] == a[1]["id"]


def test_cannot_cancel_after_start_or_reschedule_late(schemas):
    ev = make_world(schemas)
    ev.clock.advance(days=3, hours=1)  # Thu 10:00; AC booking BK-904 is Thu 11:00
    ev.new_session()
    reply = ev.say("Move my AC servicing to Saturday", {"act": "request", "goal": "reschedule",
                   "target": {"service": "ac_servicing"}, "entities": {"date": days("2026-11-07")}, "confidence": 0.9})
    assert reply.phase == "ABORTED" and "can't be moved" in reply.text and "₹399.50" in reply.text
    assert not ev.approvals() and not ev.tool_calls("reschedule")
    ev.clock.advance(hours=2)  # after the slot started
    ev.new_session()
    reply = ev.say("Cancel my AC servicing", {"act": "request", "goal": "cancel",
                   "target": {"service": "ac_servicing"}, "confidence": 0.9})
    assert not ev.approvals() and not ev.tool_calls("cancel")


def test_provider_cancel_credit_comes_from_snapshot(schemas):
    ev = make_world(schemas, "provider_cancels")
    ev.db.execute("update bookings set policy = policy || '{\"provider_cancel_credit\": 25000}' where id = 'BK-905'")
    ev.world.provider_cancels("BK-905")
    assert ev.db.scalar("select credit from refunds where booking_id = 'BK-905'") == 25000
    r = ev.db.one("select * from receipts where booking_id = 'BK-905'")
    assert r["kind"] == "provider_cancel" and r["data"]["credit"] == 25000


# --- receipts -------------------------------------------------------------------------------------


def _booked(schemas):
    ev = make_world(schemas)
    ev.new_session()
    ev.say("plumber", book(service="plumber", area="Koramangala", date=days("2026-11-04")))
    ev.say("no preference", NO_PREF)
    ev.say("1", pick(1))
    reply = ev.say("yes", YES)
    return ev, reply


def test_one_receipt_per_operation_and_renderings_agree(schemas):
    ev, reply = _booked(schemas)
    rows = ev.db.all("select * from receipts")
    assert len(rows) == 1 and rows[0]["receipt_no"] in reply.text
    r = rows[0]
    again = receipts.write(ev.db, op_key=r["op_key"], kind="book", booking_id=r["booking_id"], user_id=r["user_id"],
                           data=r["data"], now=ev.clock.now())
    assert again == r["receipt_no"] and ev.db.scalar("select count(*) from receipts") == 1
    fields = receipts.lines(r)
    as_json = receipts.as_json(r)
    text, page = receipts.as_text(r), receipts.as_html(r)
    assert [(f["label"], f["value"]) for f in as_json["fields"]] == fields
    for label, value in fields:
        assert label in text and value in text and label in page
    assert "₹" in text and r["booking_id"].startswith("BK-1")


def test_no_receipt_without_verification(schemas):
    ev = make_world(schemas, "timeout_after_success", book_outcomes=["timeout_unbooked"] * 10)
    ev.new_session()
    ev.say("plumber", book(service="plumber", area="Koramangala", date=days("2026-11-04")))
    ev.say("no preference", NO_PREF)
    ev.say("1", pick(1))
    ev.say("yes", YES)
    assert ev.db.scalar("select count(*) from receipts") == 0


def test_report_totals_match_trace(schemas):
    ev, _ = _booked(schemas)
    report = build_report(load_events(ev.db, ev.session_id))
    assert report["turns"] == 4
    assert report["tools"]["calls"] == len(ev.tool_calls())
    assert {n: v["calls"] for n, v in report["tools"]["by_name"].items()} == \
        {n: len(ev.tool_calls(n)) for n in {c["name"] for c in ev.tool_calls()}}
    assert report["model"]["calls"] == len(ev.events("model_call"))
    assert report["approvals"]["by_status"] == {"consumed": 1}
    assert report["exits"] == ["completed"] and len(report["receipts"]) == 1
