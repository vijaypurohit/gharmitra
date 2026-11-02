"""Rules parser coverage: the offline default must understand everyday phrasing, not just the examples.

Each case: (message, what the agent last asked for, expected fields). Only listed fields are checked.
Dates are relative to Mon 2 Nov 2026.
"""

import pytest

from gharmitra.llm.rules import RuleModel

WEEK = ("2026-11-02", "2026-11-08")
TUE, WED, FRI, SAT = ("2026-11-03",) * 2, ("2026-11-04",) * 2, ("2026-11-06",) * 2, ("2026-11-07",) * 2

CASES = [
    # --- requests: the brief's examples and variations
    ("Find a plumber for my kitchen tap this week, mornings only, under ₹1,500", None,
     {"act": "request", "goal": "book", "service": "plumber", "date": WEEK, "earliest": "06:00", "budget_max": 1500}),
    ("Move my AC servicing to Saturday", None, {"act": "request", "goal": "reschedule", "date": SAT}),
    ("Book the same electrician as last time, but not before 10 a.m.", None,
     {"act": "request", "goal": "book", "service": "electrician", "same_as_last": True, "earliest": "10:00"}),
    ("Cancel my cleaning booking", None, {"act": "request", "goal": "cancel"}),
    ("plumber in HSR tomorrow morning", None, {"act": "request", "goal": "book", "service": "plumber",
                                                "area": "HSR Layout", "date": TUE, "earliest": "06:00"}),
    ("My kitchen sink is leaking, can someone come tomorrow?", None,
     {"act": "request", "goal": "book", "service": "plumber", "date": TUE}),
    ("The bathroom tap is dripping", None, {"act": "request", "goal": "book", "service": "plumber"}),
    ("Fan not working, need an electrician urgently in Whitefield", None,
     {"act": "request", "goal": "book", "service": "electrician", "area": "Whitefield"}),
    ("power socket sparking", None, {"act": "request", "goal": "book", "service": "electrician"}),
    ("my AC is not cooling", None, {"act": "request", "goal": "book", "service": "ac_servicing"}),
    ("Can you get my air conditioner serviced on Friday?", None,
     {"act": "request", "goal": "book", "service": "ac_servicing", "date": FRI}),
    ("need a maid for deep cleaning on Friday in Indiranagar", None,
     {"act": "request", "goal": "book", "service": "cleaning", "area": "Indiranagar", "date": FRI}),
    ("house cleaning this weekend", None,
     {"act": "request", "goal": "book", "service": "cleaning", "date": ("2026-11-07", "2026-11-08")}),
    ("I want someone to clean my flat on 6th Nov", None, {"act": "request", "goal": "book", "service": "cleaning", "date": FRI}),
    ("schedule a plumber for wednesday evening", None,
     {"act": "request", "goal": "book", "service": "plumber", "date": WED, "earliest": "17:00"}),
    ("Please arrange an electrician between 2 and 5 pm on Wednesday", None,
     {"act": "request", "goal": "book", "service": "electrician", "date": WED, "earliest": "14:00", "latest": "17:00"}),
    ("plumber after 4pm today, budget 800", None,
     {"act": "request", "goal": "book", "service": "plumber", "date": ("2026-11-02",) * 2, "earliest": "16:00",
      "budget_max": 800}),
    ("electrician under 1k", None, {"act": "request", "goal": "book", "service": "electrician", "budget_max": 1000}),
    ("plumber for less than rs 1200 in btm", None,
     {"act": "request", "goal": "book", "service": "plumber", "area": "BTM Layout", "budget_max": 1200}),
    ("plumber in kormangala next monday", None,
     {"act": "request", "goal": "book", "service": "plumber", "area": "Koramangala", "date": ("2026-11-09",) * 2}),
    # --- Hinglish
    ("AC service karwana hai", None, {"act": "request", "goal": "book", "service": "ac_servicing"}),
    ("kal subah HSR mein plumber chahiye, 1500 tak", None,
     {"act": "request", "goal": "book", "service": "plumber", "area": "HSR Layout", "date": TUE, "earliest": "06:00",
      "budget_max": 1500}),
    ("bijli wala chahiye parso shaam", None,
     {"act": "request", "goal": "book", "service": "electrician", "date": WED, "earliest": "17:00"}),
    ("safai ke liye koi bhejo", None, {"act": "request", "goal": "book", "service": "cleaning"}),
    ("mera booking cancel karo", None, {"act": "request", "goal": "cancel"}),
    ("AC wala shanivar ko shift kar do", None, {"act": "request", "goal": "reschedule", "date": SAT}),
    # --- reschedule / cancel variations
    ("Can we push the AC service to next Tuesday?", None,
     {"act": "request", "goal": "reschedule", "date": ("2026-11-10",) * 2}),
    ("change my electrician appointment to friday", None, {"act": "request", "goal": "reschedule", "date": FRI}),
    ("I don't need the cleaner anymore", None, {"act": "request", "goal": "cancel"}),
    ("cancel booking BK-905", None, {"act": "request", "goal": "cancel", "booking_id": "BK-905"}),
    # --- answers to questions
    ("Koramangala", "required", {"act": "inform", "area": "Koramangala"}),
    ("I'm in HSR", "required", {"act": "inform", "area": "HSR Layout"}),
    ("HSR mein", "required", {"act": "inform", "area": "HSR Layout"}),
    ("kal subah", "required", {"act": "inform", "date": TUE, "earliest": "06:00"}),
    ("tomorrow works", "required", {"act": "inform", "date": TUE}),
    ("any day this week", "required", {"act": "inform", "date": WEEK}),
    ("wednesday or thursday", "required", {"act": "inform", "date": ("2026-11-04", "2026-11-05")}),
    ("no preference", "optional", {"act": "inform", "no_preference": True}),
    ("nope, anything is fine", "optional", {"act": "inform", "no_preference": True}),
    ("budget 800 max", "optional", {"act": "inform", "budget_max": 800}),
    ("after 4 pm", "optional", {"act": "inform", "earliest": "16:00"}),
    ("mornings please", "optional", {"act": "inform", "earliest": "06:00"}),
    ("make it Saturday instead", "select", {"act": "inform", "date": SAT}),
    ("actually, change the area to Jayanagar", "select", {"act": "inform", "area": "Jayanagar"}),
    ("show me cheaper options under 1000", "select", {"act": "inform", "budget_max": 1000}),
    # --- picking and confirming
    ("2", "select", {"act": "select", "option": 2}),
    ("the second one", "select", {"act": "select", "option": 2}),
    ("option 3 please", "select", {"act": "select", "option": 3}),
    ("I'll go with the first", "select", {"act": "select", "option": 1}),
    ("lets do 1", "select", {"act": "select", "option": 1}),
    ("yes", "confirm", {"act": "confirm"}),
    ("Yes, go ahead and book it", "confirm", {"act": "confirm"}),
    ("haan, book it", "confirm", {"act": "confirm"}),
    ("haan ji", "confirm", {"act": "confirm"}),
    ("confirm please", "confirm", {"act": "confirm"}),
    ("👍", "confirm", {"act": "confirm"}),
    ("ok done", "confirm", {"act": "confirm"}),
    ("no, don't", "confirm", {"act": "decline"}),
    ("not now, thanks", "confirm", {"act": "decline"}),
    ("nahi rehne do", "confirm", {"act": "decline"}),
    # --- memory and status
    ("What do you remember about me?", None, {"act": "memory", "memory_op": "view"}),
    ("show my saved preferences", None, {"act": "memory", "memory_op": "view"}),
    ("Forget my area", None, {"act": "memory", "memory_op": "forget", "memory_key": "area"}),
    ("please forget where I live", None, {"act": "memory", "memory_op": "forget", "memory_key": "area"}),
    ("change my area to BTM", None, {"act": "memory", "memory_op": "correct", "memory_key": "area"}),
    ("I live in Jayanagar, remember that", None, {"area": "Jayanagar", "remember": True}),
    ("show my bookings", None, {"act": "status"}),
    ("what's booked for me?", None, {"act": "status"}),
    ("do I have anything scheduled", None, {"act": "status"}),
    # --- not understood: must ask, never guess
    ("asdfgh", None, {"act": "unclear"}),
    ("what's the weather like", None, {"act": "unclear"}),
]


def observed(i) -> dict:
    e = i.entities
    return {
        "act": i.act, "goal": i.goal, "service": e.service, "area": e.area,
        "date": (e.date.from_.isoformat(), e.date.to.isoformat()) if e.date else None,
        "earliest": e.time.earliest.strftime("%H:%M") if e.time and e.time.earliest else None,
        "latest": e.time.latest.strftime("%H:%M") if e.time and e.time.latest else None,
        "budget_max": e.budget_max, "same_as_last": bool(e.provider and e.provider.same_as_last),
        "no_preference": e.no_preference, "remember": i.remember, "option": i.option,
        "memory_op": i.memory.op if i.memory else None, "memory_key": i.memory.key if i.memory else None,
        "booking_id": i.target.booking_id if i.target else None,
    }


@pytest.mark.parametrize("text, awaiting, expected", CASES, ids=[c[0][:40] for c in CASES])
def test_rules_understand(text, awaiting, expected):
    got = observed(RuleModel().parse(text, {"today": "2026-11-02", "awaiting": awaiting}))
    wrong = {k: got[k] for k, v in expected.items() if got[k] != v}
    assert not wrong, f"got {wrong}, want { {k: expected[k] for k in wrong} }"
