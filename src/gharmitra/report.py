"""Run report: totals for one session, computed only from its trace events."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from gharmitra.db import Database


def load_events(db: Database, session_id: str) -> list[dict[str, Any]]:
    return db.all("select session_id, seq, ts, type, payload from trace_events where session_id = %s order by seq",
                  (session_id,))


def build_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_type[e["type"]].append(e["payload"])

    models = by_type["model_call"]
    tools: dict[str, dict[str, Any]] = {}
    for c in by_type["tool_call"]:
        t = tools.setdefault(c["name"], {"calls": 0, "errors": 0, "latency_ms": 0.0})
        t["calls"] += 1
        t["errors"] += 1 if c.get("error") else 0
        t["latency_ms"] = round(t["latency_ms"] + (c.get("latency_ms") or 0), 1)

    approvals: dict[str, str] = {}
    for a in by_type["approval"]:
        approvals[a["approval_id"]] = a["status"]  # last event wins: the final status
    operations: dict[str, str] = {}
    for o in by_type["operation"]:
        operations[o["op_key"]] = o["status"]

    def ts(e: dict) -> datetime:
        v = e["ts"]
        return v if isinstance(v, datetime) else datetime.fromisoformat(v)

    return {
        "session_id": events[0]["session_id"] if events else None,
        "events": len(events),
        "duration_s": round((ts(events[-1]) - ts(events[0])).total_seconds(), 1) if events else 0.0,
        "turns": len(by_type["user_msg"]),
        "followups": len(by_type["followup"]),
        "model": {
            "calls": len(models),
            "by_adapter": dict(Counter(m["adapter"] for m in models)),
            "errors": dict(Counter(m["error"] for m in models if m.get("error"))),
            "fallbacks": sum(1 for m in models if m.get("fallback")),
            "latency_ms": round(sum(m.get("latency_ms") or 0 for m in models), 1),
            "input_tokens": sum(m.get("input_tokens") or 0 for m in models),
            "output_tokens": sum(m.get("output_tokens") or 0 for m in models),
            "cost_usd": round(sum(m.get("cost_usd") or 0 for m in models), 6),
        },
        "tools": {"calls": len(by_type["tool_call"]), "by_name": tools},
        "injection_flags": len(by_type["injection_flagged"]),
        "approvals": {"total": len(approvals), "by_status": dict(Counter(approvals.values()))},
        "gate": {"passed": sum(1 for g in by_type["gate"] if g["allowed"]),
                 "refused": sum(1 for g in by_type["gate"] if not g["allowed"])},
        "operations": operations,
        "receipts": [r["receipt_no"] for r in by_type["receipt"]] + [r["receipt_no"] for r in by_type["refund"]
                                                                     if r.get("receipt_no")],
        "exits": [x["reason"] for x in by_type["exit"]],
    }


def as_text(r: dict[str, Any]) -> str:
    m, t = r["model"], r["tools"]
    tool_parts = ", ".join(f"{n} {v['calls']}" + (f" ({v['errors']} failed)" if v["errors"] else "")
                           for n, v in sorted(t["by_name"].items()))
    lines = [
        f"Session {r['session_id']}: {r['turns']} user turns, {r['events']} events, {r['duration_s']} s",
        f"Model: {m['calls']} calls {m['by_adapter']}, {m['fallbacks']} fallbacks, {m['latency_ms']} ms"
        + (f", ${m['cost_usd']}" if m["cost_usd"] else ""),
        f"Tools: {t['calls']} calls" + (f": {tool_parts}" if tool_parts else ""),
        f"Approvals: {r['approvals']['by_status'] or 'none'}; gate passed {r['gate']['passed']},"
        f" refused {r['gate']['refused']}",
        f"Operations: {r['operations'] or 'none'}",
        f"Receipts: {', '.join(r['receipts']) or 'none'}",
        f"Injection flags: {r['injection_flags']}",
        f"Exits: {', '.join(r['exits']) or 'none'}",
    ]
    return "\n".join(lines)
