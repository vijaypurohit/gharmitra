"""Session, Task and Phase definitions + legal transition table.

The task is saved on the session after every turn, so a restart resumes in place.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from gharmitra.clock import Clock
from gharmitra.db import Database, jsonb


class Phase(StrEnum):
    COLLECTING = "COLLECTING"
    SEARCHING = "SEARCHING"
    PROPOSING = "PROPOSING"
    HOLDING = "HOLDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


P = Phase
TERMINAL = {P.DONE, P.ABORTED, P.FAILED}

# Design §3. Any phase may also stay where it is. Terminal phases start a new task in COLLECTING.
TRANSITIONS: dict[Phase, set[Phase]] = {
    P.COLLECTING: {P.SEARCHING, P.PROPOSING, P.AWAITING_APPROVAL, P.ABORTED},  # reschedule / cancel entry points
    P.SEARCHING: {P.PROPOSING, P.COLLECTING},
    P.PROPOSING: {P.HOLDING, P.AWAITING_APPROVAL, P.SEARCHING, P.COLLECTING, P.ABORTED},
    P.HOLDING: {P.AWAITING_APPROVAL, P.PROPOSING, P.SEARCHING, P.COLLECTING},
    P.AWAITING_APPROVAL: {P.EXECUTING, P.ABORTED, P.COLLECTING, P.HOLDING, P.PROPOSING, P.SEARCHING},
    P.EXECUTING: {P.VERIFYING, P.PROPOSING, P.SEARCHING, P.HOLDING, P.FAILED},
    P.VERIFYING: {P.DONE, P.EXECUTING, P.FAILED},
    P.DONE: {P.COLLECTING},
    P.ABORTED: {P.COLLECTING},
    P.FAILED: {P.COLLECTING, P.EXECUTING},  # an unconfirmed operation is reconciled on a later message
}


class IllegalTransition(Exception):
    pass


def check_transition(src: Phase, dst: Phase) -> None:
    if src != dst and dst not in TRANSITIONS[src]:
        raise IllegalTransition(f"{src} -> {dst}")


Source = Literal["user", "memory", "default"]
REQUIRED = {"book": ["service", "area", "date"], "reschedule": ["date"], "cancel": []}
OPTIONAL = ["time", "budget_max", "provider"]
SEARCH_INPUTS = {"service", "area", "date", "time", "budget_max", "provider"}


class FieldValue(BaseModel):
    value: Any = None  # None with source "user" means "no preference"
    source: Source = "user"


class Option(BaseModel):
    provider_id: str
    provider_name: str
    rating: float
    price: int  # paise, the search price
    slot: str  # ISO, local time
    reviews: list[str] = Field(default_factory=list)  # sanitised, display only


class Hold(BaseModel):
    hold_id: str
    provider_id: str
    provider_name: str
    slot: str
    quoted_price: int  # paise
    expires_at: str


class BookingRef(BaseModel):
    id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    status: str
    price: int | None = None  # paise, from the agent's copy
    area: str | None = None


class Task(BaseModel):
    goal: Literal["book", "reschedule", "cancel"] | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    optional_asked: bool = False
    memory_applied: bool = False
    options: list[Option] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)  # "provider_id|slot" known to be gone
    chosen: Option | None = None
    hold: Hold | None = None
    approval_id: str | None = None
    target: BookingRef | None = None
    candidates: list[BookingRef] = Field(default_factory=list)
    awaiting: str | None = None  # required | optional | select | confirm | pick_booking | use_inferred
    pending_fact: dict[str, str] | None = None  # inferred fact waiting for a yes / no
    declined_facts: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None  # last verified outcome, for repeated "yes"

    def value(self, key: str) -> Any:
        f = self.fields.get(key)
        return None if f is None else f.value

    def known(self, key: str) -> bool:
        return key in self.fields

    def known_fields(self) -> list[str]:
        return sorted(self.fields)


class Session(BaseModel):
    id: str
    user_id: str
    phase: Phase = P.COLLECTING
    task: Task = Field(default_factory=Task)
    turns: int = 0


class SessionStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def ensure_user(self, user_id: str, name: str | None = None) -> None:
        self.db.execute(
            "insert into users (id, name) values (%s, %s) on conflict (id) do nothing", (user_id, name or user_id)
        )

    def create(self, user_id: str, session_id: str | None = None) -> Session:
        self.ensure_user(user_id)
        session = Session(id=session_id or f"s-{uuid.uuid4().hex[:12]}", user_id=user_id)
        now = self.clock.now()
        self.db.execute(
            "insert into sessions (id, user_id, phase, task, turns, created_at, updated_at)"
            " values (%s, %s, %s, %s, 0, %s, %s)",
            (session.id, user_id, session.phase.value, jsonb(session.task.model_dump(mode="json")), now, now),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        row = self.db.one("select * from sessions where id = %s", (session_id,))
        if row is None:
            return None
        return Session(id=row["id"], user_id=row["user_id"], phase=Phase(row["phase"]),
                       task=Task.model_validate(row["task"]), turns=row["turns"])

    def save(self, session: Session) -> None:
        self.db.execute(
            "update sessions set phase = %s, task = %s, turns = %s, updated_at = %s where id = %s",
            (session.phase.value, jsonb(session.task.model_dump(mode="json")), session.turns, self.clock.now(),
             session.id),
        )

    def add_message(self, session_id: str, role: str, text: str) -> int:
        return self.db.scalar(
            "insert into messages (session_id, role, text, created_at) values (%s, %s, %s, %s) returning id",
            (session_id, role, text, self.clock.now()),
        )

    def messages(self, session_id: str) -> list[dict]:
        return self.db.all(
            "select id, role, text, created_at from messages where session_id = %s order by id", (session_id,)
        )
