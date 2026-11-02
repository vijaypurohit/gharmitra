"""Builds the model prompt: date, catalogue, phase, known field names. Never tool text, phones or addresses.

This is the only place a prompt is assembled. Its inputs are typed so tool output cannot reach it:
only the user's own message, the catalogue, the phase and the *names* of fields already known.
"""

from __future__ import annotations

import json
from datetime import date

from gharmitra.catalogue import Catalogue
from gharmitra.llm.base import Prompt, intent_schema

SYSTEM = """You turn one message from a user of a home-services booking assistant in {city} into JSON.
Reply with a single JSON object matching this schema and nothing else:
{schema}

Rules:
- act: request (asks for a booking, reschedule or cancel), inform (gives details or corrects one),
  select (picks a numbered option), confirm (yes), decline (no), memory (view, correct or forget saved
  preferences), status (asks about bookings), unclear.
- Dates are ISO (YYYY-MM-DD). Today is {today} ({weekday}). "This week" ends on Sunday.
  "Mornings" = 06:00-12:00, "afternoon" = 12:00-17:00, "evening" = 17:00-21:00.
- budget_max is in rupees. Use provider.same_as_last for "same ... as last time".
- Set remember=true only if the user asks to remember or save something.
- Services: {services}. Areas: {areas}.
- Current step: {phase}. The assistant last asked for: {awaiting}. Already known: {known}.
- Never invent values. If unsure, lower confidence."""


def build_prompt(
    text: str,
    *,
    today: date,
    catalogue: Catalogue,
    phase: str,
    known_fields: list[str],
    awaiting: str | None,
    options_shown: int = 0,
) -> Prompt:
    context = {
        "today": today.isoformat(),
        "phase": phase,
        "awaiting": awaiting,
        "known_fields": sorted(known_fields),
        "options_shown": options_shown,
    }
    system = SYSTEM.format(
        city=catalogue.city,
        schema=json.dumps(intent_schema(), separators=(",", ":")),
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        services=", ".join(catalogue.services),
        areas=", ".join(catalogue.areas),
        phase=phase,
        awaiting=awaiting or "nothing",
        known=", ".join(sorted(known_fields)) or "nothing",
    )
    return Prompt(system=system, user=text, context=context)
