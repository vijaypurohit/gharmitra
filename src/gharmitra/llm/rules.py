"""RuleModel: offline keyword parser, used by default and as the fallback."""

from __future__ import annotations

import re
from datetime import date, time, timedelta
from typing import Any

from gharmitra.catalogue import Catalogue, load_catalogue
from gharmitra.llm.base import Intent, ModelResult, Prompt

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_RE = r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|sday|nesday|rsday|urday)?"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}

YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay", "sure", "haan", "ha", "go ahead",
       "book it", "do it", "please do", "yes please", "correct", "sounds good"}
NO = {"no", "n", "nope", "nah", "decline", "don't", "dont", "stop", "nahi", "never mind", "not now", "no thanks",
      "cancel that"}
NO_PREFERENCE = {"no preference", "any", "anything", "anytime", "any time", "doesn't matter", "doesnt matter",
                 "none", "no", "nothing", "whatever", "flexible", "no preferences"}

TIME_PARTS = {"morning": (time(6), time(12)), "afternoon": (time(12), time(17)), "evening": (time(17), time(21)),
              "subah": (time(6), time(12)), "shaam": (time(17), time(21))}

MEMORY_KEYS = [
    (r"\barea\b|\blocation\b|where i live", "area"),
    (r"earliest|\btime\b|morning", "time_earliest"),
    (r"budget", "budget_max"),
]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip(" .!?")


def _clock(h: str, m: str | None, ampm: str | None) -> time | None:
    hour, minute = int(h), int(m or 0)
    ampm = (ampm or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"


class RuleModel:
    adapter = "rules"
    model = "rules-v1"

    def __init__(self, catalogue: Catalogue | None = None) -> None:
        self.catalogue = catalogue or load_catalogue()

    def complete(self, prompt: Prompt) -> ModelResult:
        intent = self.parse(prompt.user, prompt.context)
        return ModelResult(text=intent.model_dump_json(by_alias=True, exclude_none=True))

    # --- top level -------------------------------------------------------------------

    def parse(self, text: str, context: dict[str, Any] | None = None) -> Intent:
        ctx = context or {}
        today = date.fromisoformat(ctx["today"]) if ctx.get("today") else date.today()
        awaiting = ctx.get("awaiting")
        t = _clean(text)

        if (memory := self._memory(t)) is not None:
            return Intent(act="memory", memory=memory, confidence=0.9)
        if awaiting == "optional" and t in NO_PREFERENCE:
            return Intent(act="inform", entities={"no_preference": True}, confidence=0.9)
        if t in YES:
            return Intent(act="confirm", confidence=0.95)
        if t in NO:
            return Intent(act="decline", confidence=0.95)
        if (option := self._option(t)) is not None:
            return Intent(act="select", option=option, confidence=0.9)
        if re.search(r"\b(my bookings|show (my )?bookings|what have i booked|upcoming bookings|booking status)\b", t):
            return Intent(act="status", confidence=0.9)

        goal = self._goal(t)
        entities = self._entities(t, today)
        remember = bool(re.search(r"\b(remember|save (this|that|it)|always)\b", t))
        target = None
        if goal in ("reschedule", "cancel"):
            service = entities.pop("service", None)
            target = {"service": service} if service else None
            if goal == "cancel":
                entities.pop("area", None)
        if goal:
            return Intent(act="request", goal=goal, entities=entities, target=target, remember=remember, confidence=0.9)
        if entities:
            return Intent(act="inform", entities=entities, remember=remember, confidence=0.85)
        if awaiting == "optional" and re.search(r"\b(no|none|any)\b", t):
            return Intent(act="inform", entities={"no_preference": True}, confidence=0.7)
        return Intent(act="unclear", confidence=0.2)

    # --- pieces --------------------------------------------------------------------------

    def _memory(self, t: str) -> dict | None:
        if re.search(r"what do you (remember|know)( about me)?|show (my )?(memory|preferences|saved)|my preferences", t):
            return {"op": "view"}
        if m := re.match(r"forget (?:my |the )?(.+)", t):
            key = self._memory_key(m.group(1))
            return {"op": "forget", "key": key} if key else None
        m = re.match(r"(?:change|update|correct|set) my (?:saved )?(.+?) to (.+)", t)
        if m:
            key = self._memory_key(m.group(1))
            if key:
                return {"op": "correct", "key": key, "value": m.group(2).strip()}
        return None

    def _memory_key(self, phrase: str) -> str | None:
        for pattern, key in MEMORY_KEYS:
            if re.search(pattern, phrase):
                return key
        service = self.catalogue.find_service(phrase)
        if service or re.search(r"technician|provider|electrician|plumber", phrase):
            return f"provider:{service or 'electrician'}"
        return None

    @staticmethod
    def _option(t: str) -> int | None:
        if m := re.fullmatch(r"(?:option |number |no\.? |#)?([1-8])(?:st|nd|rd|th)?(?: one| option| please)?", t):
            return int(m.group(1))
        if m := re.fullmatch(r"(?:the |option |i'll take the |take the )?(first|second|third|fourth|1st|2nd|3rd|4th)(?: one| option)?(?: please)?", t):
            return ORDINALS[m.group(1)]
        if m := re.search(r"\b(?:option|number) ([1-8])\b", t):
            return int(m.group(1))
        return None

    @staticmethod
    def _goal(t: str) -> str | None:
        if re.search(r"\b(cancel|call off)\b", t):
            return "cancel"
        if re.search(r"\b(move|reschedule|shift|postpone|prepone)\b", t):
            return "reschedule"
        if re.search(r"\b(book|find|need|want|get me|looking for|search|karwana|chahiye)\b", t):
            return "book"
        return None

    def _entities(self, t: str, today: date) -> dict:
        e: dict[str, Any] = {}
        if service := self.catalogue.find_service(t):
            e["service"] = service
        if area := self.catalogue.find_area(t):
            e["area"] = area
        if d := self._dates(t, today):
            e["date"] = {"from": d[0].isoformat(), "to": d[1].isoformat()}
        if window := self._times(t):
            e["time"] = window
        if (budget := self._budget(t)) is not None:
            e["budget_max"] = budget
        if re.search(r"same (\w+ )?(as|like) (last time|before)|same (\w+ )?again|last time'?s", t):
            e["provider"] = {"same_as_last": True}
        if re.search(r"\bno preference\b", t):
            e["no_preference"] = True
        return e

    @staticmethod
    def _dates(t: str, today: date) -> tuple[date, date] | None:
        sunday = today + timedelta(days=6 - today.weekday())
        if re.search(r"day after tomorrow|parso", t):
            d = today + timedelta(days=2)
            return d, d
        if re.search(r"\b(tomorrow|tmrw|kal)\b", t):
            d = today + timedelta(days=1)
            return d, d
        if re.search(r"\btoday|aaj\b", t):
            return today, today
        if "next week" in t:
            start = sunday + timedelta(days=1)
            return start, start + timedelta(days=6)
        if "this week" in t:
            return today, sunday
        if "weekend" in t:
            sat = today + timedelta(days=(5 - today.weekday()) % 7)
            return sat, sat + timedelta(days=1)
        if m := re.search(rf"\b(next )?{_WEEKDAY_RE}\b", t):
            wanted = [w[:3] for w in WEEKDAYS].index(m.group(2)[:3])
            d = today + timedelta(days=(wanted - today.weekday()) % 7)
            if m.group(1) and d <= sunday:
                d += timedelta(days=7)
            return d, d
        months = "|".join(MONTHS)
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? ({months})[a-z]*\b", t) or re.search(
            rf"\b({months})[a-z]* (\d{{1,2}})(?:st|nd|rd|th)?\b", t
        )
        if m:
            a, b = m.groups()
            day, mon = (int(a), b) if a.isdigit() else (int(b), a)
            try:
                d = date(today.year, MONTHS.index(mon[:3]) + 1, day)
            except ValueError:
                return None
            if d < today:
                d = d.replace(year=today.year + 1)
            return d, d
        return None

    @staticmethod
    def _times(t: str) -> dict | None:
        window: dict[str, str] = {}
        for word, (start, end) in TIME_PARTS.items():
            if re.search(rf"\b{word}s?\b", t):
                window = {"earliest": start.isoformat("minutes"), "latest": end.isoformat("minutes")}
        if m := re.search(rf"\b(?:not before|after|from|no earlier than|earliest)\s+{_TIME}", t) or re.search(
            rf"\b{_TIME}\s+(?:onwards|or later)\b", t
        ):
            if (c := _clock(*m.groups())) is not None:
                window["earliest"] = c.isoformat("minutes")
        if m := re.search(rf"\b(?<!not )(?:before|by|not after|no later than|latest)\s+{_TIME}", t):
            if (c := _clock(*m.groups())) is not None:
                window["latest"] = c.isoformat("minutes")
        return window or None

    @staticmethod
    def _budget(t: str) -> int | None:
        m = re.search(
            r"\b(?:under|below|less than|within|max(?:imum)?|up ?to|budget(?: of| is)?|not more than)\s*"
            r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b",
            t,
        ) or re.search(r"(?:₹|rs\.?\s?)(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t)
        if not m:
            return None
        value = float(m.group(1).replace(",", ""))
        if m.group(2):
            value *= 1000
        return int(value)
