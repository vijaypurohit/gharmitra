"""Model adapter contract (tier 2, step 17): every adapter returns Intent JSON plus usage, and maps
timeouts, 429, non-zero exits and bad output to ModelError so the turn falls back to rules."""

import json
import stat
import sys
from datetime import date

import httpx
import httpx2
import pytest

from gharmitra.agent.budget import Budget
from gharmitra.agent.intents import IntentParser
from gharmitra.catalogue import load_catalogue
from gharmitra.clock import FakeClock
from gharmitra.config import Limits, Settings
from gharmitra.llm.anthropic_api import AnthropicModel
from gharmitra.llm.base import ModelError, ModelResult, Usage
from gharmitra.llm.claude_cli import ClaudeCliModel
from gharmitra.llm.factory import Paced, make_model, probe
from gharmitra.llm.openai_api import OpenAIModel
from gharmitra.llm.prompt import build_prompt
from gharmitra.llm.rules import RuleModel
from gharmitra.trace import MemorySink, Tracer

INTENT = {"act": "request", "goal": "book", "entities": {"service": "plumber", "area": "HSR Layout"}, "confidence": 0.9}
PROMPT = build_prompt("plumber in HSR tomorrow", today=date(2026, 11, 2), catalogue=load_catalogue(),
                      phase="COLLECTING", known_fields=[], awaiting=None)
KEY = "sk-test-contract-0123456789"


def parse_with(model):
    """Run the real IntentParser (schema validation + fallback) over an adapter."""
    sink = MemorySink()
    clock = FakeClock()
    parser = IntentParser(model, RuleModel(), load_catalogue(), Tracer("t", [sink], clock))
    parsed = parser.parse("plumber in HSR tomorrow", today=date(2026, 11, 2), phase="COLLECTING", known_fields=[],
                          awaiting=None, budget=Budget(Limits(), clock))
    return parsed, [e["payload"] for e in sink.events if e["type"] == "model_call"]


# --- openai (OpenAI-compatible HTTP) ------------------------------------------------------------------


def openai_model(handler, base_url=None):
    s = Settings(model_adapter="openai", model_name="m-1", model_api_key=KEY, model_base_url=base_url)
    return OpenAIModel(s, transport=httpx.MockTransport(handler))


def chat_reply(content, usage=None):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                     "usage": usage or {"prompt_tokens": 700, "completion_tokens": 30}})


@pytest.mark.parametrize("base, url", [
    (None, "https://api.openai.com/v1/chat/completions"),
    ("https://generativelanguage.googleapis.com/v1beta/openai/",
     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
])
def test_openai_request_shape_and_urls(base, url):
    seen = []

    def handler(req):
        seen.append(req)
        return chat_reply(json.dumps(INTENT))

    result = openai_model(handler, base).complete(PROMPT)
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == url and req.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "m-1" and body["messages"][1] == {"role": "user", "content": "plumber in HSR tomorrow"}
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert "minimum" not in json.dumps(fmt) and fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert json.loads(result.text) == INTENT and (result.usage.input_tokens, result.usage.output_tokens) == (700, 30)


def test_openai_falls_back_to_json_object_when_schema_rejected():
    formats = []

    def handler(req):
        fmt = json.loads(req.content)["response_format"]["type"]
        formats.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "Request contains an invalid argument."}})
        return chat_reply("```json\n" + json.dumps(INTENT) + "\n```")

    model = openai_model(handler)
    assert json.loads(model.complete(PROMPT).text) == INTENT  # fences stripped
    model.complete(PROMPT)
    assert formats == ["json_schema", "json_object", "json_object"]  # remembered


@pytest.mark.parametrize("respond, kind", [
    (lambda req: httpx.Response(429, text="Too Many Requests"), "rate_limited"),
    (lambda req: httpx.Response(503, text="high demand"), "rate_limited"),
    (lambda req: httpx.Response(500, text="boom"), "http_error"),
    (lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)), "timeout"),
    (lambda req: httpx.Response(200, json={"nope": 1}), "bad_output"),
])
def test_openai_errors_map_to_model_errors(respond, kind):
    with pytest.raises(ModelError) as exc:
        openai_model(respond).complete(PROMPT)
    assert exc.value.kind == kind and KEY not in str(exc.value)


def test_openai_bad_json_falls_back_to_rules():
    parsed, calls = parse_with(openai_model(lambda req: chat_reply("I think you want a plumber!")))
    assert parsed.fallback and parsed.intent.entities.service == "plumber"
    assert [(c["adapter"], c["error"]) for c in calls] == [("openai", "bad_output"), ("rules", None)]


def test_openai_requires_model_name():
    with pytest.raises(ValueError):
        OpenAIModel(Settings(model_adapter="openai"))


# --- anthropic (official SDK, stubbed transport) -------------------------------------------------------


def anthropic_model(handler, name=None):
    s = Settings(model_adapter="anthropic", model_name=name, model_api_key=KEY)
    return AnthropicModel(s, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def message(text, stop="end_turn"):
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 50}})


def test_anthropic_request_shape_usage_and_cost():
    seen = []

    def handler(req):
        seen.append(req)
        return message(json.dumps(INTENT))

    model = anthropic_model(handler)
    result = model.complete(PROMPT)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/messages"
    assert KEY in (seen[0].headers.get("x-api-key", "") + seen[0].headers.get("authorization", ""))
    assert body["model"] == "claude-sonnet-5-5" and body["system"] == PROMPT.system
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "minimum" not in json.dumps(body["output_config"])
    assert json.loads(result.text) == INTENT
    assert result.usage == Usage(1000, 50, round((1000 * 2 + 50 * 10) / 1e6, 6))


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (529, "http_error"), (400, "http_error")])
def test_anthropic_status_errors(status, kind):
    def handler(req):
        return httpx2.Response(status, headers={"retry-after": "0"},
                               json={"type": "error", "error": {"type": "x", "message": "nope"}})

    with pytest.raises(ModelError) as exc:
        anthropic_model(handler).complete(PROMPT)
    assert exc.value.kind == kind


def test_anthropic_timeout_and_refusal():
    def slow(req):
        raise httpx2.ReadTimeout("slow", request=req)

    with pytest.raises(ModelError) as exc:
        anthropic_model(slow).complete(PROMPT)
    assert exc.value.kind == "timeout"
    with pytest.raises(ModelError) as exc:
        anthropic_model(lambda req: message("", stop="refusal")).complete(PROMPT)
    assert exc.value.kind == "bad_output"


def test_anthropic_no_effort_for_older_models():
    seen = []
    anthropic_model(lambda req: seen.append(req) or message(json.dumps(INTENT)), "claude-haiku-4-5").complete(PROMPT)
    assert "effort" not in json.loads(seen[0].content)["output_config"]


# --- claude_cli (fake `claude` script) ----------------------------------------------------------------

FAKE = r'''import json, os, sys, time
mode = os.environ.get("FAKE_CLAUDE", "ok")
args = sys.argv[1:]
stdin = sys.stdin.read()
json.dump({{"args": args, "stdin": stdin, "cwd": os.getcwd()}}, open(os.environ["FAKE_LOG"], "w"))
if mode == "ok":
    print(json.dumps({{"type": "result", "is_error": False, "result": "",
                      "structured_output": {intent},
                      "usage": {{"input_tokens": 900, "output_tokens": 40}}, "total_cost_usd": 0.0012}}))
elif mode == "error":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Claude usage limit reached"}}))
elif mode == "exit":
    sys.stderr.write("not logged in"); sys.exit(1)
elif mode == "slow":
    time.sleep(5)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    body = tmp_path / "fake_claude.py"
    body.write_text(FAKE.format(intent=json.dumps(INTENT)))
    script = tmp_path / "claude"  # a sh wrapper: a shebang can't hold a path with spaces
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return str(script), log


def test_claude_cli_runs_isolated_and_reads_structured_output(fake_claude, monkeypatch):
    script, log = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE", "ok")
    result = ClaudeCliModel(Settings(model_adapter="claude_cli"), command=script).complete(PROMPT)
    assert json.loads(result.text) == INTENT and result.usage == Usage(900, 40, 0.0012)
    seen = json.loads(log.read_text())
    args = seen["args"]
    assert seen["stdin"] == "plumber in HSR tomorrow"  # the message is never an argument
    assert args[:3] == ["-p", "--output-format", "json"]
    for flag, value in (("--tools", ""), ("--setting-sources", ""), ("--model", "sonnet")):
        assert args[args.index(flag) + 1] == value
    assert "--no-session-persistence" in args and "--strict-mcp-config" in args
    assert json.loads(args[args.index("--json-schema") + 1])["additionalProperties"] is False
    assert "gm-claude-" in seen["cwd"]


@pytest.mark.parametrize("mode, kind", [("error", "rate_limited"), ("exit", "process_error"), ("slow", "timeout")])
def test_claude_cli_failures(fake_claude, monkeypatch, mode, kind):
    script, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE", mode)
    model = ClaudeCliModel(Settings(model_adapter="claude_cli", model_timeout_s=1), command=script)
    with pytest.raises(ModelError) as exc:
        model.complete(PROMPT)
    assert exc.value.kind == kind


def test_claude_cli_missing_binary():
    model = ClaudeCliModel(Settings(model_adapter="claude_cli"), command=None)
    model.command = None
    with pytest.raises(ModelError) as exc:
        model.complete(PROMPT)
    assert exc.value.kind == "process_error"


# --- registry and pacing --------------------------------------------------------------------------------


class StubModel:
    adapter, model = "stub", "stub-1"

    def __init__(self, settings):
        self.settings = settings

    def complete(self, prompt):
        return ModelResult(json.dumps(INTENT), Usage(1, 1, 0.0))


def test_registry_builtins_custom_class_and_fallback():
    assert make_model(Settings()).adapter == "rules"
    assert make_model(Settings(model_adapter="tests.unit.test_adapters:StubModel")).adapter == "stub"
    assert make_model(Settings(model_adapter="anthropic", model_api_key=KEY)).model == "claude-sonnet-5-5"
    assert make_model(Settings(model_adapter="openai")).adapter == "rules"  # no MODEL_NAME: warn, use rules
    with pytest.raises(ValueError):
        make_model(Settings(model_adapter="nope"))
    r = probe(Settings(model_adapter="openai"), "plumber")
    assert r["ok"] is False and r["error"] == "config"
    r = probe(Settings(model_adapter="tests.unit.test_adapters:StubModel"), "plumber")
    assert r["ok"] and r["intent"]["goal"] == "book"


def test_pacing_keeps_min_interval():
    t = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    paced = Paced(StubModel(Settings()), 12.0, sleep=sleep, now=lambda: t[0])
    paced.complete(PROMPT)
    t[0] += 5
    paced.complete(PROMPT)
    t[0] += 20
    paced.complete(PROMPT)
    assert slept == [7.0] and paced.adapter == "stub"
    assert isinstance(make_model(Settings(model_adapter="tests.unit.test_adapters:StubModel",
                                          model_min_interval_s=12)), Paced)
