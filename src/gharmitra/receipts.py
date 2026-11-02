"""Receipts: one per verified book, reschedule or cancel (and per provider-cancel refund).

A receipt is written in the same transaction that records the verified outcome, so there is never a
receipt without a verification, and `op_key` is unique, so never two for one operation. JSON, HTML
and text renderings show the same fields.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from gharmitra.db import Database, jsonb
from gharmitra.money import format_inr

TITLES = {"book": "Booking", "cancel": "Cancellation", "reschedule": "Reschedule",
          "provider_cancel": "Provider cancellation refund"}


def write(db: Database, *, op_key: str, kind: str, booking_id: str, user_id: str, data: dict[str, Any],
          now: datetime) -> str:
    """Call inside the transaction that records the verified outcome. Returns the receipt number."""
    existing = db.scalar("select receipt_no from receipts where op_key = %s", (op_key,))
    if existing:
        return existing
    receipt_no = f"GM-{now:%Y%m%d}-{db.scalar('select nextval(%s)', ('receipt_seq',)):04d}"
    db.execute(
        "insert into receipts (receipt_no, op_key, kind, booking_id, user_id, data, created_at)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (receipt_no, op_key, kind, booking_id, user_id, jsonb(data), now),
    )
    return receipt_no


def get(db: Database, receipt_no: str) -> dict[str, Any] | None:
    return db.one("select * from receipts where receipt_no = %s", (receipt_no,))


def lines(r: dict[str, Any]) -> list[tuple[str, str]]:
    """The fields every rendering shows, in order."""
    d = r["data"]
    rows = [("Receipt", r["receipt_no"]), ("Type", TITLES[r["kind"]]), ("Booking", r["booking_id"] or "")]
    rows += [("Provider", d["provider_name"]), ("Service", d.get("service_label") or d.get("service", "")),
             ("Slot", d["slot_label"])]
    if d.get("previous_slot_label"):
        rows.append(("Previous slot", d["previous_slot_label"]))
    if r["kind"] in ("book", "reschedule"):
        rows.append(("Price", format_inr(d["price"])))
    else:
        rows += [("Price paid", format_inr(d["price"])), ("Fee", format_inr(d.get("fee", 0))),
                 ("Refund", format_inr(d.get("refund", 0)))]
        if d.get("credit"):
            rows.append(("Credit", format_inr(d["credit"])))
        if d.get("policy_note"):
            rows.append(("Policy", d["policy_note"]))
    rows.append(("Issued", r["created_at"].isoformat(timespec="seconds")))
    return rows


def as_json(r: dict[str, Any]) -> dict[str, Any]:
    return {"receipt_no": r["receipt_no"], "kind": r["kind"], "booking_id": r["booking_id"],
            "op_key": r["op_key"], "issued_at": r["created_at"].isoformat(), **r["data"],
            "fields": [{"label": k, "value": v} for k, v in lines(r)]}


def as_text(r: dict[str, Any]) -> str:
    width = max(len(k) for k, _ in lines(r))
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in lines(r))


def as_html(r: dict[str, Any]) -> str:
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in lines(r))
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Receipt {html.escape(r['receipt_no'])}</title>"
        "<link rel=\"stylesheet\" href=\"/static/style.css\"></head>"
        f"<body class=\"receipt\"><main><h1>GharMitra · {html.escape(TITLES[r['kind']])}</h1>"
        f"<table>{rows}</table><p class=\"muted\">Mock booking for demonstration; no payment was taken.</p>"
        "</main></body></html>"
    )
