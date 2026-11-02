"""ProviderAPI: search_providers, get_provider, list_bookings, hold_slot, book, get_booking_status, cancel, reschedule.

The mock keeps its world in the `mock` schema. Each client is bound to one user, like an
authenticated API client. Prices cross the API in whole rupees (the contract's "price (₹)").
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from gharmitra.catalogue import Catalogue, load_catalogue
from gharmitra.clock import Clock
from gharmitra.db import Database, jsonb
from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.scenarios import Scenario

HOLD_MINUTES = 10
SLOT_HOURS = range(8, 19)
SLOTS_PER_DAY = 4


class ToolError(Exception):
    """A failure reported by the provider API. `code` is the contract's failure name."""

    code = "error"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message or code or self.code)
        if code:
            self.code = code


class SlotUnavailable(ToolError):
    code = "slot_unavailable"


class SlotTaken(ToolError):
    code = "slot_taken"


class HoldExpired(ToolError):
    code = "hold_expired"


class SlotConflict(ToolError):
    code = "slot_conflict"


class ServiceUnavailable(ToolError):
    code = "503"


class ToolTimeout(ToolError):
    code = "timeout"


class NotFound(ToolError):
    code = "not_found"


class MockFault(ToolError):
    code = "internal_error"


_FAILURES = {c.code: c for c in (SlotUnavailable, SlotTaken, HoldExpired, SlotConflict, NotFound)}


class ProviderAPI(Protocol):
    def search_providers(self, service: str, area: str, date_range: tuple[str, str]) -> list[dict]: ...
    def get_provider(self, provider_id: str) -> dict: ...
    def list_bookings(self, user_id: str) -> list[dict]: ...
    def hold_slot(self, provider_id: str, slot: str) -> dict: ...
    def book(self, hold_id: str, idempotency_key: str) -> dict: ...
    def get_booking_status(self, idempotency_key: str) -> dict: ...
    def cancel(self, booking_id: str, idempotency_key: str) -> dict: ...
    def reschedule(self, booking_id: str, new_slot: str, idempotency_key: str) -> dict: ...


def _iso(dt: datetime, tz: Any) -> str:
    return dt.astimezone(tz).isoformat()


def _rupees(paise: int) -> int:
    return paise // 100


class MockProviderAPI:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        scenario: Scenario,
        user_id: str,
        catalogue: Catalogue | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.scenario = scenario
        self.user_id = user_id
        self.catalogue = catalogue or load_catalogue()
        self.tz = clock.tz
        self.rng = random.Random(f"{scenario.seed}:outcomes")
        self._hold_calls: dict[str, int] = {}

    # --- seeding ---------------------------------------------------------------

    def seed(self) -> bool:
        """Create the provider world once. Returns False if it already exists."""
        if self.db.scalar("select count(*) from providers"):
            return False
        s = self.scenario
        today = self.clock.now()
        with self.db.tx() as conn:
            for service, rows in sd.PROVIDERS.items():
                for i, (pid, name, price, rating, attrs) in enumerate(rows):
                    attrs = list(attrs)
                    if s.injected_names and pid == sd.INJECTED_NAME[0]:
                        name = sd.INJECTED_NAME[1]
                        attrs.append(sd.INJECTED_ATTRIBUTE[1])
                    conn.execute(
                        "insert into providers (id, name, service, areas, price, rating, attributes, phone, address)"
                        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (pid, name, service, sd.provider_areas(self.catalogue, i), price * 100, rating,
                         jsonb(attrs), sd.phone_for(s.seed, pid), f"{10 + i}, {i + 1}st Cross, Bengaluru"),
                    )
                    rng = random.Random(f"{s.seed}:reviews:{pid}")
                    reviews = rng.sample(sd.GOOD_REVIEWS, 2)
                    if pid in sd.INJECTION_REVIEWS:
                        reviews.insert(1, sd.INJECTION_REVIEWS[pid])
                    for j, text in enumerate(reviews):
                        conn.execute(
                            "insert into reviews (provider_id, author, rating, text) values (%s, %s, %s, %s)",
                            (pid, f"Customer {j + 1}", rng.randint(3, 5), text),
                        )
            for n, b in enumerate(sd.PAST_BOOKINGS):
                conn.execute(
                    "insert into bookings (id, user_id, provider_id, service, slot_start, status, created_at)"
                    " values (%s, %s, %s, %s, %s, %s, %s)",
                    (f"BK-{901 + n}", b.user_id, b.provider_id, b.service, sd.past_slot(today, b), b.status, today),
                )
        return True

    # --- slots -------------------------------------------------------------------

    def _schedule(self, provider_id: str, day: date) -> list[datetime]:
        rng = random.Random(f"{self.scenario.seed}:slots:{provider_id}:{day.isoformat()}")
        hours = sorted(rng.sample(list(SLOT_HOURS), SLOTS_PER_DAY))
        return [datetime.combine(day, time(h), tzinfo=self.tz) for h in hours]

    def _taken(self, provider_id: str, start: datetime, end: datetime) -> set[datetime]:
        now = self.clock.now()
        rows = self.db.all(
            "select slot_start from bookings where provider_id = %s and status = 'booked'"
            " and slot_start between %s and %s"
            " union select slot_start from holds where provider_id = %s and not used and expires_at > %s"
            " and user_id <> %s and slot_start between %s and %s",
            (provider_id, start, end, provider_id, now, self.user_id, start, end),
        )
        return {r["slot_start"] for r in rows}

    def free_slots(self, provider_id: str, first: date, last: date) -> list[datetime]:
        earliest = self.clock.now() + timedelta(hours=1)
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        slots = [s for d in days for s in self._schedule(provider_id, d) if s >= earliest]
        if not slots:
            return []
        taken = self._taken(provider_id, slots[0], slots[-1])
        return [s for s in slots if s not in taken]

    def _provider(self, provider_id: str) -> dict:
        row = self.db.one("select * from providers where id = %s", (provider_id,))
        if row is None:
            raise NotFound(f"provider {provider_id} not found")
        return row

    def _maybe_fail(self, tool: str) -> None:
        if tool in self.scenario.fail_tools:
            raise MockFault("Internal error 0x5f3: upstream returned malformed response")

    # --- read-only tools -----------------------------------------------------------

    def search_providers(self, service: str, area: str, date_range: tuple[str, str]) -> list[dict]:
        self._maybe_fail("search_providers")
        first, last = (date.fromisoformat(d) for d in date_range)
        rows = self.db.all(
            "select * from providers where service = %s and %s = any(areas) order by id", (service, area)
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "price": _rupees(r["price"]),
                "rating": float(r["rating"]),
                "free_slots": [_iso(s, self.tz) for s in self.free_slots(r["id"], first, last)],
                "attributes": r["attributes"],
            }
            for r in rows[:8]
        ]

    def get_provider(self, provider_id: str) -> dict:
        self._maybe_fail("get_provider")
        r = self._provider(provider_id)
        reviews = self.db.all(
            "select author, rating, text from reviews where provider_id = %s order by id", (provider_id,)
        )
        return {
            "id": r["id"],
            "name": r["name"],
            "service": r["service"],
            "price": _rupees(r["price"]),
            "rating": float(r["rating"]),
            "areas": r["areas"],
            "attributes": r["attributes"],
            "phone": r["phone"],
            "address": r["address"],
            "reviews": reviews,
        }

    def list_bookings(self, user_id: str) -> list[dict]:
        self._maybe_fail("list_bookings")
        rows = self.db.all(
            "select b.id, b.provider_id, p.name as provider_name, b.service, b.slot_start, b.status"
            " from bookings b join providers p on p.id = b.provider_id where b.user_id = %s order by b.slot_start",
            (user_id,),
        )
        return [
            {"id": r["id"], "provider_id": r["provider_id"], "provider_name": r["provider_name"],
             "service": r["service"], "slot": _iso(r["slot_start"], self.tz), "status": r["status"]}
            for r in rows
        ]

    # --- holds -----------------------------------------------------------------------

    def hold_slot(self, provider_id: str, slot: str) -> dict:
        self._maybe_fail("hold_slot")
        p = self._provider(provider_id)
        start = datetime.fromisoformat(slot)
        day = start.astimezone(self.tz).date()
        if (provider_id, _iso(start, self.tz)) in self.scenario.unavailable_slots or start not in self.free_slots(
            provider_id, day, day
        ):
            raise SlotUnavailable(f"{slot} is not available")
        calls = self._hold_calls.get(provider_id, 0)
        self._hold_calls[provider_id] = calls + 1
        drift = self.scenario.drift_for(provider_id, calls)
        if drift is None:
            drift = 0
            if self.rng.random() < self.scenario.drift_probability:
                drift = self.rng.randint(1, max(1, self.scenario.drift_max // 50)) * 50
        quoted = p["price"] + drift * 100
        now = self.clock.now()
        hold_id = "H-%d" % self.db.scalar("select nextval('hold_seq')")
        expires = now + timedelta(minutes=HOLD_MINUTES)
        self.db.execute(
            "insert into holds (id, provider_id, user_id, slot_start, quoted_price, expires_at)"
            " values (%s, %s, %s, %s, %s, %s)",
            (hold_id, provider_id, self.user_id, start, quoted, expires),
        )
        return {"hold_id": hold_id, "expires_at": _iso(expires, self.tz), "quoted_price": _rupees(quoted)}

    # --- side effects ------------------------------------------------------------------

    def _replay(self, key: str, op: str) -> dict | None:
        row = self.db.one("select status, result from idempotency where key = %s and op = %s", (key, op))
        if row is None:
            return None
        if row["status"] == "failed":
            code = row["result"]["reason"]
            raise _FAILURES.get(code, ToolError)(f"{op} failed: {code}", code=code)
        return row["result"]

    def _record(self, key: str, op: str, status: str, result: dict) -> None:
        self.db.execute(
            "insert into idempotency (key, op, status, result, created_at) values (%s, %s, %s, %s, %s)",
            (key, op, status, jsonb(result), self.clock.now()),
        )

    def _fail(self, key: str, op: str, error: ToolError) -> ToolError:
        self._record(key, op, "failed", {"reason": error.code})
        return error

    def _create_booking(self, hold: dict, user_id: str, key: str | None) -> str:
        p = self._provider(hold["provider_id"])
        booking_id = "BK-%d" % self.db.scalar("select nextval('booking_seq')")
        self.db.execute(
            "insert into bookings (id, user_id, provider_id, service, slot_start, status, idempotency_key, created_at)"
            " values (%s, %s, %s, %s, %s, 'booked', %s, %s)",
            (booking_id, user_id, hold["provider_id"], p["service"], hold["slot_start"], key, self.clock.now()),
        )
        self.db.execute("update holds set used = true where id = %s", (hold["id"],))
        return booking_id

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        self._maybe_fail("book")
        # Failures are recorded, committed, then raised, so a repeated key replays them.
        error: ToolError | None = None
        with self.db.tx():
            replay = self._replay(idempotency_key, "book")
            if replay is not None:
                return {"booking_id": replay["booking_id"]}
            hold = self.db.one("select * from holds where id = %s and user_id = %s", (hold_id, self.user_id))
            if hold is None:
                raise NotFound(f"hold {hold_id} not found")
            outcome = None
            if hold["used"]:
                error = self._fail(idempotency_key, "book", SlotTaken("hold already used"))
            elif self.clock.now() > hold["expires_at"]:
                error = self._fail(idempotency_key, "book", HoldExpired(f"hold {hold_id} expired"))
            else:
                outcome = self.scenario.next_book_outcome() or self._random_outcome()
                if outcome == "error":
                    raise MockFault("Internal error 0x2a: booking service crashed")
                if outcome == "slot_taken":
                    self._create_booking(hold, sd.OTHER_USER, None)
                    error = self._fail(idempotency_key, "book", SlotTaken("slot was taken by another customer"))
                elif outcome in ("ok", "timeout_booked"):
                    booking_id = self._create_booking(hold, self.user_id, idempotency_key)
                    self._record(idempotency_key, "book", "booked", {"booking_id": booking_id})
        if error is not None:
            raise error
        if outcome == "ok":
            return {"booking_id": booking_id}
        self.clock.sleep(self.scenario.timeout_seconds)
        raise ToolTimeout(f"book timed out after {self.scenario.timeout_seconds:.0f}s")

    def _random_outcome(self) -> str:
        r = self.rng.random()
        if r < 0.70:
            return "ok"
        if r < 0.85:
            return "slot_taken"
        return "timeout_booked" if self.rng.random() < 0.5 else "timeout_not_booked"

    def get_booking_status(self, idempotency_key: str) -> dict:
        self._maybe_fail("get_booking_status")
        row = self.db.one("select status, result from idempotency where key = %s and op = 'book'", (idempotency_key,))
        if row is None:
            return {"status": "not_found"}
        if row["status"] == "failed":
            return {"status": "failed", "reason": row["result"]["reason"]}
        return {"status": "booked", "booking_id": row["result"]["booking_id"]}

    def _own_booking(self, booking_id: str) -> dict:
        row = self.db.one("select * from bookings where id = %s and user_id = %s", (booking_id, self.user_id))
        if row is None:
            raise NotFound(f"booking {booking_id} not found")
        return row

    def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        self._maybe_fail("cancel")
        with self.db.tx():
            replay = self._replay(idempotency_key, "cancel")
            if replay is not None:
                return replay
            b = self._own_booking(booking_id)
            calls = self.db.scalar(
                "insert into cancel_calls (booking_id, calls) values (%s, 1)"
                " on conflict (booking_id) do update set calls = cancel_calls.calls + 1 returning calls",
                (booking_id,),
            )
        if self.scenario.cancel_503_first and calls == 1:
            raise ServiceUnavailable("503 Service Unavailable, try again")
        if b["status"] != "booked":
            raise self._fail(idempotency_key, "cancel", ToolError(f"booking is {b['status']}", code="not_cancellable"))
        with self.db.tx():
            self.db.execute("update bookings set status = 'cancelled' where id = %s", (booking_id,))
            result = {"status": "cancelled", "booking_id": booking_id}
            self._record(idempotency_key, "cancel", "cancelled", result)
        return result

    def reschedule(self, booking_id: str, new_slot: str, idempotency_key: str) -> dict:
        self._maybe_fail("reschedule")
        replay = self._replay(idempotency_key, "reschedule")
        if replay is not None:
            return replay
        b = self._own_booking(booking_id)
        start = datetime.fromisoformat(new_slot)
        day = start.astimezone(self.tz).date()
        if (
            _iso(start, self.tz) in self.scenario.conflict_slots
            or b["status"] != "booked"
            or start not in self.free_slots(b["provider_id"], day, day)
        ):
            raise self._fail(idempotency_key, "reschedule", SlotConflict(f"{new_slot} conflicts"))
        with self.db.tx():
            self.db.execute("update bookings set slot_start = %s where id = %s", (start, booking_id))
            p = self._provider(b["provider_id"])
            result = {"id": booking_id, "provider_id": b["provider_id"], "provider_name": p["name"],
                      "service": b["service"], "slot": _iso(start, self.tz), "status": "booked"}
            self._record(idempotency_key, "reschedule", "rescheduled", result)
        return result

    # --- demo hook -----------------------------------------------------------------------

    def provider_cancels(self, booking_id: str) -> dict:
        """Not part of the agent's tool set: simulates the provider cancelling a booking."""
        self.db.execute("update bookings set status = 'provider_cancelled' where id = %s", (booking_id,))
        return {"booking_id": booking_id, "status": "provider_cancelled"}
