"""Side-effecting operations: write-ahead log, idempotency keys, retries, status verification, restart reconciliation.

One approval → one operation → one `op_key` = `{approval_id}:{action}`. The operation row is
committed before the call. A key is retried only while its outcome is unknown (timeout, 503), and
every retry passes the gate again. A definite failure ends the operation. Success is recorded only
after verification: `get_booking_status` for book, `list_bookings` for cancel and reschedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gharmitra import receipts
from gharmitra.agent.approval import ApprovalError, Approvals, Values
from gharmitra.agent.tools import Tools
from gharmitra.clock import Clock
from gharmitra.config import Limits
from gharmitra.db import Database, jsonb
from gharmitra.mock_api.provider import ServiceUnavailable, ToolError, ToolTimeout
from gharmitra.trace import Tracer

EXPIRING = {"hold_expired", "approval_expired"}


class SimulatedCrash(BaseException):
    """Raised at a crash point in evals. A BaseException, so no handler turns it into a reply."""


@dataclass
class Outcome:
    status: str  # succeeded | failed | unknown
    action: str
    approval_id: str
    op_key: str
    booking_id: str | None = None
    error: str | None = None
    resumed: bool = False
    receipt_no: str | None = None


def op_key(approval_id: str, action: str) -> str:
    return f"{approval_id}:{action}"


class Executor:
    def __init__(
        self,
        db: Database,
        approvals: Approvals,
        clock: Clock,
        tracer: Tracer,
        limits: Limits,
        crash_points: set[str] | None = None,
    ) -> None:
        self.db = db
        self.approvals = approvals
        self.clock = clock
        self.tracer = tracer
        self.limits = limits
        self.crash_points = crash_points if crash_points is not None else set()

    # --- operation rows ------------------------------------------------------------

    def _crash(self, point: str) -> None:
        if point in self.crash_points:
            self.crash_points.discard(point)
            raise SimulatedCrash(point)

    def _set(self, op: dict, **changes: Any) -> dict:
        changes["updated_at"] = self.clock.now()
        sets = ", ".join(f"{k} = %({k})s" for k in changes)
        row = self.db.one(f"update operations set {sets} where op_key = %(key)s returning *", {**changes, "key": op["op_key"]})
        if "status" in changes:
            self.tracer.emit("operation", op_key=row["op_key"], status=row["status"], calls=row["calls"],
                             booking_id=row["booking_id"], error=row["error"])
        return row

    def operation_for(self, approval_id: str) -> dict | None:
        return self.db.one("select * from operations where approval_id = %s", (approval_id,))

    def open_operations(self, session_id: str) -> list[dict]:
        return self.db.all(
            "select * from operations where session_id = %s and status in ('started', 'unknown') order by created_at",
            (session_id,),
        )

    # --- entry points ----------------------------------------------------------------

    def execute(self, approval_id: str, values: Values, tools: Tools, session_id: str) -> Outcome:
        """Gate → write-ahead → call → verify."""
        existing = self.operation_for(approval_id)
        if existing is not None:
            return self.resume(existing, values, tools)
        self.approvals.check(approval_id, values)
        key = op_key(approval_id, values.action)
        now = self.clock.now()
        with self.db.tx():
            op = self.db.one(
                "insert into operations (op_key, approval_id, session_id, action, status, created_at, updated_at)"
                " values (%s, %s, %s, %s, 'started', %s, %s) returning *",
                (key, approval_id, session_id, values.action, now, now),
            )
        self.tracer.emit("operation", op_key=key, status="started", calls=0, booking_id=None, error=None)
        self._crash("after_write_ahead")
        return self._drive(op, values, tools, check_first=False, gated=True)

    def resume(self, op: dict, values: Values, tools: Tools) -> Outcome:
        """Restart or retry path: always ask the provider what happened before calling again."""
        out = self._drive(op, values, tools, check_first=True, gated=False)
        out.resumed = True
        return out

    # --- the loop --------------------------------------------------------------------------

    def _drive(self, op: dict, values: Values, tools: Tools, check_first: bool, gated: bool) -> Outcome:
        """`gated` is True only right after execute() checked the gate; every other call checks again."""
        key = op["op_key"]
        check = check_first or op["status"] == "unknown"
        retries = 0
        while True:
            if check:
                verdict, detail = self._status(op, values, tools)
                if verdict == "done":
                    return self._succeed(op, values, detail)
                if verdict == "failed":
                    return self._fail(op, detail or "failed")
            if op["calls"] > 0:
                retries += 1
                if retries > self.limits.retries_per_operation:
                    op = self._set(op, status="unknown")
                    return Outcome("unknown", values.action, op["approval_id"], key, error="retries_exhausted")
                self.clock.sleep(min(2 ** (retries - 1), 4))
                tools.budget.check_time()
            if not gated:
                try:
                    self.approvals.check(op["approval_id"], values)
                except ApprovalError as exc:
                    reason = "approval_expired" if exc.reason == "expired" else f"gate_{exc.reason}"
                    return self._fail(op, reason)
            gated = False
            op = self._set(op, calls=op["calls"] + 1)
            try:
                result = self._call(values, key, tools)
            except ToolTimeout:
                op = self._set(op, status="unknown")
                self._crash("after_timeout")
                check = True
                continue
            except ServiceUnavailable:
                check = False
                continue
            except ToolError as exc:
                return self._fail(op, exc.code)
            # The call returned; still verify before claiming success.
            if values.action == "book":
                op = self._set(op, booking_id=result["booking_id"])
            check = True

    def _call(self, v: Values, key: str, tools: Tools) -> Any:
        if v.action == "book":
            return tools.book(hold_id=v.hold_id, idempotency_key=key)
        if v.action == "cancel":
            return tools.cancel(booking_id=v.booking_id, idempotency_key=key)
        return tools.reschedule(booking_id=v.booking_id, new_slot=v.slot, idempotency_key=key)

    def _status(self, op: dict, v: Values, tools: Tools) -> tuple[str, str | None]:
        """("done", booking_id) · ("failed", reason) · ("not_found", None)."""
        if v.action == "book":
            st = tools.get_booking_status(idempotency_key=op["op_key"])
            if st["status"] == "booked":
                return "done", st["booking_id"]
            if st["status"] == "failed":
                return "failed", st.get("reason", "failed")
            return "not_found", None
        bookings = tools.list_bookings(user_id=v.meta["user_id"])
        b = next((b for b in bookings if b["id"] == v.booking_id), None)
        if b is None:
            return "failed", "booking_not_found"
        if v.action == "cancel" and b["status"] == "cancelled":
            return "done", v.booking_id
        if v.action == "reschedule" and datetime.fromisoformat(b["slot"]) == datetime.fromisoformat(v.slot):
            return "done", v.booking_id
        return "not_found", None

    # --- endings ---------------------------------------------------------------------------

    def _succeed(self, op: dict, v: Values, booking_id: str | None) -> Outcome:
        with self.db.tx():
            op = self._set(op, status="succeeded", booking_id=booking_id)
            self.approvals.consume(op["approval_id"])
            receipt_no = self._record(v, booking_id, op["op_key"])
        self.tracer.emit("receipt", receipt_no=receipt_no, op_key=op["op_key"], kind=v.action, booking_id=booking_id)
        return Outcome("succeeded", v.action, op["approval_id"], op["op_key"], booking_id=booking_id,
                       receipt_no=receipt_no)

    def _record(self, v: Values, booking_id: str | None, key: str) -> str:
        """The agent's copy of the booking and its receipt, written with the operation's success."""
        now = self.clock.now()
        m = v.meta
        data = {"provider_id": v.provider_id, "provider_name": m.get("provider_name", v.provider_id),
                "service": m.get("service", ""), "service_label": m.get("service_label", ""),
                "slot": v.slot, "slot_label": m.get("slot_label", v.slot)}
        if v.action == "book":
            self.db.execute(
                "insert into bookings (id, user_id, provider_id, provider_name, service, area, slot_start, price,"
                " status, policy, op_key, created_at, updated_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, 'booked', %s, %s, %s, %s) on conflict (id) do nothing",
                (booking_id, m["user_id"], v.provider_id, data["provider_name"], data["service"],
                 m.get("area"), datetime.fromisoformat(v.slot), v.amount, jsonb(m.get("policy", {})), key, now, now),
            )
            data["price"] = v.amount
        elif v.action == "cancel":
            self.db.execute("update bookings set status = 'cancelled', updated_at = %s where id = %s", (now, booking_id))
            self.db.execute(
                "insert into refunds (booking_id, amount, fee, op_key, created_at) values (%s, %s, %s, %s, %s)",
                (booking_id, v.amount, m.get("fee", 0), key, now),
            )
            data.update(price=m.get("price", v.amount), fee=m.get("fee", 0), refund=v.amount,
                        tier=m.get("tier"), policy_note=m.get("policy_note"))
        else:
            self.db.execute(
                "update bookings set slot_start = %s, updated_at = %s where id = %s",
                (datetime.fromisoformat(v.slot), now, booking_id),
            )
            data.update(price=v.amount, previous_slot=m.get("previous_slot"),
                        previous_slot_label=m.get("previous_slot_label"))
        return receipts.write(self.db, op_key=key, kind=v.action, booking_id=booking_id or "",
                              user_id=m["user_id"], data=data, now=now, tz=self.clock.tz)

    def _fail(self, op: dict, reason: str) -> Outcome:
        with self.db.tx():
            op = self._set(op, status="failed", error=reason)
            if reason in EXPIRING:
                self.approvals.expire(op["approval_id"], reason)
            else:
                self.approvals.void(op["approval_id"], reason)
        return Outcome("failed", op["action"], op["approval_id"], op["op_key"], error=reason)
