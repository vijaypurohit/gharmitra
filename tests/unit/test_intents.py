from datetime import date

import pytest
from pydantic import ValidationError

from gharmitra.agent.budget import Budget, LimitExceeded
from gharmitra.agent.intents import IntentParser, normalise
from gharmitra.catalogue import load_catalogue
from gharmitra.clock import FakeClock
from gharmitra.config import Limits
from gharmitra.llm.base import Intent, ModelError
from gharmitra.llm.rules import RuleModel
from gharmitra.llm.scripted import ScriptedModel, ScriptMiss
from gharmitra.trace import MemorySink, Tracer

TODAY = date(2026, 11, 2)  # Monday
CTX = {"today": TODAY.isoformat()}
rules = RuleModel()


def parse(text, **ctx):
    return rules.parse(text, {**CTX, **ctx}).model_dump(by_alias=True, exclude_defaults=True, mode="json")


# --- rules parser: the brief's three examples and the README dialogue ---------------


def test_plumber_this_week_mornings_under_budget():
    i = parse("Find a plumber for my kitchen tap this week, mornings only, under ₹1,500")
    assert i["act"] == "request" and i["goal"] == "book"
    assert i["entities"] == {
        "service": "plumber",
        "date": {"from": "2026-11-02", "to": "2026-11-08"},
        "time": {"earliest": "06:00:00", "latest": "12:00:00"},
        "budget_max": 1500,
    }


def test_move_ac_to_saturday():
    i = parse("Move my AC servicing to Saturday")
    assert (i["act"], i["goal"], i["target"]) == ("request", "reschedule", {"service": "ac_servicing"})
    assert i["entities"]["date"] == {"from": "2026-11-07", "to": "2026-11-07"}


def test_same_electrician_not_before_10():
    i = parse("Book the same electrician as last time, but not before 10 a.m.")
    assert (i["goal"], i["entities"]["service"]) == ("book", "electrician")
    assert i["entities"]["provider"] == {"same_as_last": True}
    assert i["entities"]["time"] == {"earliest": "10:00:00"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Koramangala", {"act": "inform", "entities": {"area": "Koramangala"}}),
        ("1", {"act": "select", "option": 1}),
        ("option 2", {"act": "select", "option": 2}),
        ("the second one", {"act": "select", "option": 2}),
        ("yes", {"act": "confirm"}),
        ("no", {"act": "decline"}),
        ("make it Saturday", {"act": "inform", "entities": {"date": {"from": "2026-11-07", "to": "2026-11-07"}}}),
        ("What do you remember about me?", {"act": "memory", "memory": {"op": "view"}}),
        ("Forget my area", {"act": "memory", "memory": {"op": "forget", "key": "area"}}),
        ("change my area to HSR", {"act": "memory", "memory": {"op": "correct", "key": "area", "value": "hsr"}}),
        ("Cancel my cleaning booking", {"act": "request", "goal": "cancel", "target": {"service": "cleaning"}}),
        ("show my bookings", {"act": "status"}),
    ],
)
def test_dialogue_turns(text, expected):
    i = parse(text)
    for key, value in expected.items():
        assert i[key] == value, (key, i)


def test_service_without_a_verb_is_a_booking_request():
    assert parse("plumber in HSR tomorrow morning")["goal"] == "book"
    assert parse("plumber", awaiting="required")["act"] == "inform"


def test_no_means_no_preference_when_asked_for_optional_fields():
    assert parse("no", awaiting="optional") == {"act": "inform", "entities": {"no_preference": True}, "confidence": 0.9}
    assert parse("no preference")["entities"] == {"no_preference": True}


@pytest.mark.parametrize(
    "text, window",
    [
        ("after 10am", {"earliest": "10:00:00"}),
        ("before 11 am", {"latest": "11:00:00"}),
        ("evening", {"earliest": "17:00:00", "latest": "21:00:00"}),
        ("from 2 pm", {"earliest": "14:00:00"}),
    ],
)
def test_time_windows(text, window):
    assert parse(text)["entities"]["time"] == window


def test_dates_and_budget_forms():
    assert parse("tomorrow")["entities"]["date"]["from"] == "2026-11-03"
    assert parse("next saturday")["entities"]["date"]["from"] == "2026-11-14"
    assert parse("on 5 nov")["entities"]["date"]["from"] == "2026-11-05"
    assert parse("budget 1.5k")["entities"]["budget_max"] == 1500
    assert parse("asdf qwerty")["act"] == "unclear"


# --- schema ------------------------------------------------------------------------


def test_schema_has_no_way_to_express_price_or_approval():
    for extra in ({"approved": True}, {"price": 99}, {"tool": "book"}, {"entities": {"quoted_price": 1}}):
        with pytest.raises(ValidationError):
            Intent.model_validate({"act": "confirm", **extra})


# --- normalisation --------------------------------------------------------------------

cat = load_catalogue()


def test_normalise_aliases_and_unknowns():
    i, problems = normalise(Intent.model_validate({"act": "inform", "entities": {"area": "HSR", "service": "AC service"}}), cat, TODAY)
    assert (i.entities.area, i.entities.service, problems) == ("HSR Layout", "ac_servicing", {})
    i, problems = normalise(Intent.model_validate({"act": "inform", "entities": {"area": "Marathahalli"}}), cat, TODAY)
    assert i.entities.area is None and problems == {"area": "Marathahalli"}


def test_normalise_dates_future_and_within_30_days():
    def d(a, b):
        return normalise(Intent.model_validate({"act": "inform", "entities": {"date": {"from": a, "to": b}}}), cat, TODAY)

    assert d("2026-10-01", "2026-10-05")[1] == {"date": "past"}
    assert d("2026-12-25", "2026-12-26")[1] == {"date": "too_far"}
    i, problems = d("2026-10-30", "2026-12-30")
    assert (i.entities.date.from_, i.entities.date.to, problems) == (TODAY, date(2026, 12, 2), {})


# --- parser: budget, tracing and fallback ----------------------------------------------


def make_parser(model, limits=Limits()):
    sink = MemorySink()
    clock = FakeClock()
    parser = IntentParser(model, RuleModel(), cat, Tracer("s", [sink], clock))
    return parser, sink, Budget(limits, clock)


def run(parser, budget, text):
    return parser.parse(text, today=TODAY, phase="COLLECTING", known_fields=[], awaiting=None, budget=budget)


@pytest.mark.parametrize("bad", ["not json", '{"act": "confirm", "approved": true}', ModelError("rate_limited", "429")])
def test_bad_model_output_falls_back_to_rules(bad):
    parser, sink, budget = make_parser(ScriptedModel({"plumber in HSR tomorrow": bad}))
    parsed = run(parser, budget, "plumber in HSR tomorrow")
    assert parsed.fallback and parsed.adapter == "rules"
    assert parsed.intent.entities.area == "HSR Layout"
    calls = [e["payload"] for e in sink.events if e["type"] == "model_call"]
    assert [(c["adapter"], c["fallback"], c["error"] is None) for c in calls] == [("scripted", False, False), ("rules", True, True)]
    assert budget.model_calls == 2


def test_low_confidence_is_not_usable():
    parser, _, budget = make_parser(ScriptedModel({"hmm": {"act": "confirm", "confidence": 0.4}}))
    assert run(parser, budget, "hmm").usable is False


def test_script_miss_raises_instead_of_falling_back():
    parser, _, budget = make_parser(ScriptedModel({}))
    with pytest.raises(ScriptMiss):
        run(parser, budget, "unscripted")


def test_model_cap_stops_before_the_call():
    parser, sink, budget = make_parser(ScriptedModel({"x": {"act": "confirm"}}), Limits(model_calls_per_turn=1))
    run(parser, budget, "x")
    with pytest.raises(LimitExceeded) as e:
        run(parser, budget, "x")
    assert e.value.reason == "limit_model_calls"
    assert sum(1 for ev in sink.events if ev["type"] == "model_call") == 1


def test_scripted_model_records_prompts_without_tool_text():
    model = ScriptedModel({"x": {"act": "confirm"}})
    parser, _, budget = make_parser(model)
    run(parser, budget, "x")
    p = model.prompts[0]
    assert "2026-11-02" in p.system and "HSR Layout" in p.system and p.user == "x"


def test_rules_parse_booking_id_target():
    from gharmitra.llm.rules import RuleModel

    i = RuleModel().parse("Cancel booking BK-905", {"today": "2026-11-02"})
    assert i.goal == "cancel" and i.target.booking_id == "BK-905"
