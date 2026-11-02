"""Eval fixtures and the session-end hook that writes docs/eval_table.md (only when evals ran)."""

from __future__ import annotations

import pytest

from tests.evals import eval_table
from tests.evals.harness import check_invariants

RESULTS: dict[str, dict] = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "eval(id, name, expected, required=False): an eval scenario row")


class Recorder:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.ev = None

    def world(self, ev):
        self.ev = ev
        return ev

    def check(self, *, notes: str = "") -> None:
        """Record the actual summary, then assert the global invariants."""
        if self.ev is None:
            return
        self.row["actual"] = self.ev.summary()
        problems = check_invariants(self.ev)
        self.row["notes"] = "; ".join(filter(None, [notes, *problems]))
        assert not problems, problems


@pytest.fixture
def record(request) -> Recorder:
    marker = request.node.get_closest_marker("eval")
    row = RESULTS.setdefault(request.node.nodeid, _row(marker))
    return Recorder(row)


def _row(marker) -> dict:
    eval_id, name, expected = marker.args[:3]
    return {"id": eval_id, "name": name, "expected": expected, "required": marker.kwargs.get("required", False),
            "outcome": "passed", "actual": "", "notes": ""}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("eval")
    if marker is None:
        return
    row = RESULTS.setdefault(item.nodeid, _row(marker))
    if report.skipped:
        row["outcome"] = "skipped"
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        row["notes"] = reason.removeprefix("Skipped: ")
    elif report.failed:
        row["outcome"] = "error" if report.when != "call" else "failed"
        message = str(report.longrepr).strip().splitlines()[-1][:200]
        if message not in row["notes"]:
            row["notes"] = "; ".join(filter(None, [row["notes"], message]))


def pytest_sessionfinish(session, exitstatus):
    if RESULTS:
        eval_table.write(list(RESULTS.values()))
