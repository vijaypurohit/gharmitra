"""Proactive follow-up when a provider cancels a booking (design §10).

The cancel event is checked against the provider before anything is recorded. Then the full refund
and the ₹100 credit are written, and a replacement search runs in a new session. The agent stops at
the options: a replacement is booked only through the user's pick and the normal approval gate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from gharmitra.agent.state import BookingRef, FieldValue
from gharmitra.money import format_inr, rupees

if TYPE_CHECKING:
    from gharmitra.agent.controller import Controller, Reply, Turn

CANCEL_CREDIT = rupees(100)  # mirrors the late-cancellation fee
REPLACEMENT_DAYS = 2  # search the original day and the next two
MAX_DAYS_AHEAD = 30


def provider_cancelled(controller: Controller, booking_id: str) -> Reply | None:
    """Handle a provider-cancel event. Returns the follow-up turn, or None if there is nothing to do
    (unknown booking, or this event was already handled)."""
    copy = controller.deps.db.one("select * from bookings where id = %s", (booking_id,))
    if copy is None or copy["status"] != "booked":
        return None
    session_id = controller.start_session(copy["user_id"])
    return controller.system_turn(session_id, "provider_cancelled", lambda t: _follow_up(controller, t, copy),
                                  booking_id=booking_id)


def _follow_up(c: Controller, t: Turn, copy: dict) -> None:
    booking_id = copy["id"]
    live = next((b for b in t.tools.list_bookings(user_id=t.user) if b["id"] == booking_id), None)
    if live is None or live["status"] != "provider_cancelled":
        # the event is not trusted on its own: the provider still shows the booking, so nothing changes
        t.tracer.emit("followup_ignored", booking_id=booking_id, provider_status=live and live["status"])
        t.say(f"I checked booking {booking_id} after a cancellation notice, and it is still active. Nothing changed.")
        return

    db, now = c.deps.db, t.clock.now()
    with db.tx():
        db.execute("update bookings set status = 'provider_cancelled', updated_at = %s where id = %s", (now, booking_id))
        db.execute("insert into refunds (booking_id, amount, fee, credit, op_key, created_at)"
                   " values (%s, %s, 0, %s, %s, %s)",
                   (booking_id, copy["price"], CANCEL_CREDIT, f"provider_cancel:{booking_id}", now))
    t.tracer.emit("refund", booking_id=booking_id, amount=copy["price"], fee=0, credit=CANCEL_CREDIT,
                  reason="provider_cancelled")

    slot = copy["slot_start"].astimezone(t.clock.tz)
    today = now.astimezone(t.clock.tz).date()
    start = max(slot.date(), today)
    end = min(start + timedelta(days=REPLACEMENT_DAYS), today + timedelta(days=MAX_DAYS_AHEAD))
    task = t.task
    task.goal = "book"
    task.replaces = BookingRef(id=booking_id, provider_id=copy["provider_id"], provider_name=copy["provider_name"],
                               service=copy["service"], slot=slot.isoformat(), status="provider_cancelled",
                               price=copy["price"], area=copy["area"])
    area, source = copy["area"], "user"
    if not area:
        area, source = t.memory.stated(t.user, "area"), "memory"
    task.fields = {
        "service": FieldValue(value=copy["service"]),
        "date": FieldValue(value={"from": start.isoformat(), "to": end.isoformat()}),
        **({"area": FieldValue(value=area, source=source)} if area else {}),
    }
    task.memory_applied = True  # the cancelled booking is the source, not saved preferences
    task.optional_asked = True

    t.say(f"{copy['provider_name']} cancelled your {t.label(copy['service'])} booking for "
          f"{t.slot(slot.isoformat())} ({booking_id}). I've recorded a full refund of {format_inr(copy['price'])} "
          f"and a {format_inr(CANCEL_CREDIT)} credit. I haven't booked anything else.")
    if not area:
        task.awaiting = "required"
        t.say("Which area should I look for a replacement in?")
        return
    c._search(t)

