"""Claude Code support tests: the Anthropic Messages wire format.

Kept in their own file, matching ``backend/claudecode/``, so Codex's tests and
these cannot be broken by the same edit.

Two layers, same as ``test_routing.py``:

1. HERMETIC -- the real FastAPI app with a spy provider in place of OpenCode, so
   every assertion is about what actually reached the model and what actually
   came back, not about a mock being called.
2. LIVE -- one real request per effort rung against the real free tier, to prove
   the translated dial changes the model's behaviour rather than being accepted
   and ignored. Skipped when the network or the free tier is unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from typing import List
from pathlib import Path

# Isolated data dir: these tests must never touch the real ledger or key store.
os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-cc-test-"))
os.environ["LINGLING_ACCOUNTS_FILE"] = os.path.join(
    os.environ["LINGLING_DATA_DIR"], "accounts.json"
)
os.environ["LINGLING_BOOTSTRAP_TOR"] = "0"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config  # noqa: E402
from providers.base import Provider  # noqa: E402
from providers.key_pool import KeyPool  # noqa: E402
from routing import dispatcher  # noqa: E402

import app as app_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MODEL = "deepseek-v4-flash-free"

# The real catalog methods, captured before any test replaces them. `app` is a
# module-level singleton shared with tests/test_routing.py in one pytest process,
# so a stub left behind here breaks the live tests over there -- which is exactly
# what happened once. Restored after every test by the fixture below.
_REAL_CATALOG = {
    name: getattr(app_mod.catalog, name)
    for name in ("providers_for", "by_id", "free", "refresh", "is_burned", "is_blacklisted", "is_free")
}


def _restore_catalog() -> None:
    for name, value in _REAL_CATALOG.items():
        setattr(app_mod.catalog, name, value)


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _catalog_isolation():
        """Undo every catalog stub, so this file cannot leak into another."""
        yield
        _restore_catalog()
except ImportError:      # running the file directly, without pytest
    pass


class SkipTest(unittest.SkipTest):
    """Raised by a test to mark itself skipped."""


class _QuietLog:
    """Logger stand-in: the stream watchdog logs a stall, and the test output
    should stay readable.
    """

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class SpyProvider(Provider):
    """Records what reached the provider, then answers with a canned reply.

    Not a mock: the request travels the whole real path -- handler, bridge,
    routing, effort resolution, executor -- and this is the far end of it. What it
    records is therefore what OpenCode would have received.
    """

    display_name = "spy"
    base_url = "http://spy.invalid"

    def __init__(self, answer=None, stream_frames=None):
        super().__init__(KeyPool([]))
        self.id = "spy"
        self.sent = []
        self._answer = answer or {
            "id": "chatcmpl-spy",
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "4"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
        }
        self._frames = stream_frames

    def requires_key(self):
        return False

    def needs_proxy(self):
        return False

    def prefer_direct(self, model_id):
        return True

    def fetch_model_ids(self):
        return [MODEL]

    def is_model_free(self, model_id, meta):
        return True

    def chat_completions(self, messages, model, secret, timeout=None, **params):
        self.sent.append({"model": model, "messages": messages, "params": params})
        return self._answer

    def stream_chat(self, messages, model, secret, timeout=None, **params):
        self.sent.append({"model": model, "messages": messages, "params": params})
        frames = self._frames or [
            b'data: {"choices":[{"delta":{"content":"4"},"finish_reason":"stop"}]}',
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":1}}',
            b"data: [DONE]",
        ]
        yield from frames


def _install(spy, effort_values=("high", "max"), vision=False):
    """Point the running app at a spy provider and a model with known caps.

    Returns a TestClient. The catalog is replaced rather than mocked at the
    provider layer so routing, vision stripping and effort clamping all see a
    coherent model.
    """
    def fake_model():
        return type("M", (), {
            "id": MODEL, "name": "Spy Model", "vision": vision,
            "reasoning": True, "context_length": 200000, "provider_ids": ["spy"],
            "capabilities": {"effort": list(effort_values)},
            # /v1/models serialises through to_dict; the model picker and the
            # setup window both read that endpoint.
            "to_dict": lambda self: {
                "id": MODEL, "name": "Spy Model", "free": True, "vision": vision,
                "reasoning": True, "context_length": 200000,
                "provider_ids": ["spy"], "capabilities": {"effort": list(effort_values)},
            },
        })()

    app_mod.catalog.providers_for = lambda mid: [spy] if mid == MODEL else []
    app_mod.catalog.by_id = lambda mid: type("M", (), {
        "vision": vision, "id": mid, "capabilities": {"effort": list(effort_values)},
    })()
    app_mod.catalog.free = lambda: [fake_model()]
    app_mod.catalog.refresh = lambda force=False: None
    app_mod.catalog.is_burned = lambda mid: False
    app_mod.catalog.is_blacklisted = lambda mid: False
    app_mod.catalog.is_free = lambda mid: True if mid == MODEL else None
    return TestClient(app_mod.app)


# ---------------------------------------------------------------------------
# 1. Effort translation -- the reason this endpoint exists
# ---------------------------------------------------------------------------
def test_unit_claude_effort_reaches_the_model_it_routed_to():
    """Every Claude Code effort rung must arrive as a value the model implements.

    Claude Code and OpenCode use overlapping but different vocabularies, and each
    free model publishes its own sparse set. deepseek publishes ['high','max'], so
    `low` has to clamp *up* to `high` rather than be forwarded -- OpenCode answers
    200 for a value a model does not implement, which would look like the dial
    worked while changing nothing.
    """
    spy = SpyProvider()
    client = _install(spy, effort_values=("high", "max"))

    for label in ("low", "medium", "high", "xhigh", "max"):
        spy.sent.clear()
        r = client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 64,
            "messages": [{"role": "user", "content": "2+2?"}],
            "output_config": {"effort": label},
        })
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
        got = spy.sent[0]["params"].get("reasoning_effort")
        assert got in ("high", "max"), f"effort {label!r} reached the model as {got!r}"

    # A model with a full ladder keeps each rung distinct -- proof the clamping is
    # per-model rather than a fixed mapping.
    ling = SpyProvider()
    client = _install(ling, effort_values=("low", "medium", "high"))
    got = {}
    for label in ("low", "medium", "high", "xhigh", "max"):
        ling.sent.clear()
        client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": label},
        })
        got[label] = ling.sent[0]["params"].get("reasoning_effort")
    assert (got["low"], got["medium"], got["high"]) == ("low", "medium", "high"), got
    assert got["xhigh"] == got["max"] == "high", got


def test_unit_claude_thinking_budget_becomes_an_effort_rung():
    """The older ``thinking.budget_tokens`` control must translate too.

    Claude Code still sends a token budget when MAX_THINKING_TOKENS is set or when
    it believes the model predates adaptive thinking. A budget is a number, not a
    word, so it is bucketed onto the same ladder -- otherwise the whole older
    control silently does nothing.
    """
    spy = SpyProvider()
    client = _install(spy, effort_values=("low", "medium", "high"))

    # Claude Code's own presets cluster near 4k, 10k and 32k tokens.
    for budget, expected in ((1024, "low"), (4_000, "medium"),
                             (10_000, "high"), (32_000, "high"), (60_000, "high")):
        spy.sent.clear()
        client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": budget},
        })
        got = spy.sent[0]["params"].get("reasoning_effort")
        assert got == expected, f"budget {budget} -> {got!r}, expected {expected!r}"

    # A bigger budget must never resolve to *less* thinking than a smaller one.
    from claudecode import effort as cc_effort
    ladder = [cc_effort.requested_effort(
        {"thinking": {"type": "enabled", "budget_tokens": b}}
    ) for b in (1024, 4_000, 10_000, 32_000, 60_000)]
    from routing import effort as shared
    ranks = [shared._RANKS[label] for label in ladder]
    assert ranks == sorted(ranks), f"budget ladder is not monotonic: {list(zip(ladder, ranks))}"


def test_unit_claude_effort_is_dropped_for_a_model_with_no_dial():
    """mimo/nemotron/big-pickle publish no effort values; they must get none.

    OpenCode returns 200 for an effort value a model ignores. Forwarding one is
    therefore worse than dropping it: the request looks like it honoured the
    client's setting when nothing about the generation changed.
    """
    spy = SpyProvider()
    client = _install(spy, effort_values=())

    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"effort": "max"},
    })
    assert "reasoning_effort" not in spy.sent[0]["params"], spy.sent[0]["params"]


def test_unit_claude_explicit_no_thinking_is_not_silence():
    """``thinking: {"type": "disabled"}`` must reach a model that can turn off.

    Saying nothing leaves the model at its default depth, which for a reasoning
    model means it still thinks. A client that explicitly disabled thinking has
    asked for something different, and a model publishing an off rung can honour
    it.
    """
    spy = SpyProvider()
    client = _install(spy, effort_values=("none", "high"))

    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "disabled"},
    })
    assert spy.sent[0]["params"].get("reasoning_effort") == "none"

    # `adaptive` is a thinking *mode*, not a depth. Anthropic's docs say so
    # explicitly, and ranking it as an unknown word would drop the real depth
    # that sits alongside it in output_config.
    spy.sent.clear()
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    })
    assert spy.sent[0]["params"].get("reasoning_effort") == "high"


# ---------------------------------------------------------------------------
# 2. Request translation
# ---------------------------------------------------------------------------
def test_unit_claude_request_shape_becomes_a_chat_conversation():
    """The four structural differences from chat completions, in one request.

    Anthropic has no system role, carries tool traffic inside content blocks, and
    replays its own thinking blocks. Each of those needs moving, and getting any
    of them wrong produces a request the model answers *badly* rather than
    rejecting -- which is the hard kind of bug to notice.
    """
    spy = SpyProvider()
    client = _install(spy)

    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 100,
        "system": "You are terse.",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "read a.py"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "I should read it", "signature": "sig"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "tu_1", "name": "read_file",
                 "input": {"path": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "print(1)"},
                {"type": "text", "text": "what does it do?"},
            ]},
        ],
        "tools": [{"name": "read_file", "description": "Read a file",
                   "input_schema": {"type": "object",
                                    "properties": {"path": {"type": "string"}}}}],
        "tool_choice": {"type": "auto"},
        "stop_sequences": ["STOP"],
    })
    assert r.status_code == 200, r.text[:300]
    built = spy.sent[0]
    messages = built["messages"]

    # The system prompt is a top-level field in Anthropic and must become a turn.
    assert messages[0] == {"role": "system", "content": "You are terse."}
    # A tool result answers the previous assistant turn, so it precedes the new
    # user text rather than being merged into it.
    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "tool", "user"], [m["role"] for m in messages]
    assert messages[3]["tool_call_id"] == "tu_1"
    assert messages[3]["content"] == "print(1)"
    assert messages[4]["content"] == "what does it do?"

    # tool_use -> tool_calls, with `input` re-serialised as a JSON *string*.
    call = messages[2]["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}

    # A replayed thinking block must come back as `reasoning_content` on the
    # assistant turn. Dropping it -- which an earlier version did -- made the
    # upstream reject the next tool turn outright with "The `reasoning_content` in
    # the thinking mode must be passed back to the API", killing the agent loop.
    assert messages[2]["reasoning_content"] == "I should read it"

    # input_schema is Anthropic's name for what chat completions calls parameters.
    fn = built["params"]["tools"][0]["function"]
    assert fn["parameters"]["properties"]["path"]["type"] == "string"
    assert "input_schema" not in fn
    assert built["params"]["tool_choice"] == "auto"
    assert built["params"]["max_tokens"] == 16384
    assert built["params"]["stop"] == ["STOP"]


def test_unit_claude_max_tokens_floor_prevents_reasoning_exhaustion():
    """A low max_tokens from the client must be bumped to prevent reasoning
    models from spending their entire budget on thinking with zero content.

    Claude Code sends a conservative max_tokens default.  Deepseek at max
    effort fills the reasoning budget first, so a low value like 4096 leaves
    no room for an answer.  OpenCode natively defaults higher; this floor
    matches that behaviour so the same model works through Lingling.
    """
    spy = SpyProvider()
    client = _install(spy)

    # A request with a very low max_tokens.
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert spy.sent[0]["params"]["max_tokens"] == 16384, \
        "low max_tokens must be bumped to the floor"

    # Already above the floor — untouched.
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 32000,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert spy.sent[1]["params"]["max_tokens"] == 32000, \
        "high max_tokens must pass through unchanged"


def test_unit_claude_system_prompt_survives_either_documented_shape():
    """``system`` is ``string | TextBlockParam[]`` and both must arrive intact.

    Claude Code sends the array form when it attaches cache_control breakpoints to
    parts of the prompt. Handling only the string form silently dropped the entire
    system prompt for those requests -- the model would answer with no
    instructions at all and look merely unhelpful.
    """
    spy = SpyProvider()
    client = _install(spy)

    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 32,
        "system": [
            {"type": "text", "text": "You are a coding agent."},
            {"type": "text", "text": "Prefer small diffs.",
             "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    })
    system = spy.sent[0]["messages"][0]
    assert system["role"] == "system"
    assert "coding agent" in system["content"] and "small diffs" in system["content"]


def test_unit_claude_images_survive_the_hop():
    """A screenshot must reach a vision model, in the shape chat completions wants.

    Anthropic sends base64 with a separate media_type; chat completions wants one
    data URI. Dropping the image turns "what's wrong in this screenshot" into an
    unanswerable question, and vision_bridge would then route it to a text-only
    model -- two failures from one missing conversion.
    """
    spy = SpyProvider()
    client = _install(spy, vision=True)

    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "iVBORw0KGgo="}},
        ]}],
    })
    content = spy.sent[0]["messages"][0]["content"]
    assert isinstance(content, list), content
    image = [p for p in content if p.get("type") == "image_url"]
    assert image, content
    assert image[0]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgo="

    # The URL form is passed through unchanged.
    spy.sent.clear()
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}},
        ]}],
    })
    content = spy.sent[0]["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == "https://x.test/a.png"


# ---------------------------------------------------------------------------
# 3. Response translation
# ---------------------------------------------------------------------------
def test_unit_claude_response_is_a_real_anthropic_message():
    """The response must satisfy a client that validates Anthropic's schema.

    Every field here is one a client reads: ``type``/``role`` to recognise the
    object, ``content`` as a block list, ``stop_reason`` to decide whether the turn
    is over, and ``usage`` under Anthropic's own field names.
    """
    spy = SpyProvider()
    client = _install(spy)

    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "2+2?"}],
    }).json()

    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "4"}]
    assert body["stop_reason"] == "end_turn"
    assert body["stop_sequence"] is None
    # The model asked for is echoed, not the one that ran: a client compares this
    # against its own request. The real target is reported additively.
    assert body["model"] == MODEL
    assert body["lingling"]["routed_model"] == MODEL
    assert body["usage"]["input_tokens"] == 7
    assert body["usage"]["output_tokens"] == 1
    # Present-and-zero rather than absent: Claude Code reads these when accounting
    # for a session, and a missing key is a KeyError where 0 is just "no caching".
    assert body["usage"]["cache_creation_input_tokens"] == 0
    assert body["usage"]["cache_read_input_tokens"] == 0


def test_unit_claude_stop_reason_uses_anthropic_vocabulary():
    """``finish_reason`` and ``stop_reason`` are different words for the same idea.

    An agent branches on stop_reason: ``tool_use`` means run the tool,
    ``max_tokens`` means the answer was cut off. Leaving OpenAI's words in place
    means every branch takes the wrong path.
    """
    from claudecode import messages_response as mr

    assert mr.stop_reason("stop") == "end_turn"
    assert mr.stop_reason("length") == "max_tokens"
    assert mr.stop_reason("tool_calls") == "tool_use"
    assert mr.stop_reason("content_filter") == "refusal"
    # Unknown and missing values must not raise -- an unfamiliar upstream word
    # should still produce a turn a client can finish reading.
    assert mr.stop_reason("something_new") == "end_turn"
    assert mr.stop_reason(None) == "end_turn"

    # A turn carrying a tool call is a tool_use turn even when the upstream said
    # `stop`. Some free models finish a tool call without setting the reason, and
    # an agent that reads end_turn there never runs the tool.
    out = mr.response_object({
        "choices": [{"finish_reason": "stop", "message": {
            "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ls", "arguments": "{}"}}]}}],
    }, "m", "m", "spy")
    assert out["stop_reason"] == "tool_use", out


def test_unit_claude_tool_call_becomes_a_tool_use_block():
    """Arguments cross back from a JSON string to a parsed object.

    Anthropic's ``input`` is a real object where chat completions uses a string.
    A model that emits malformed JSON must not take the whole response down: the
    call still reaches the client, with an empty input, so the agent can see what
    was attempted.
    """
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "Looking.",
            "tool_calls": [{"id": "call_1", "function": {
                "name": "read_file", "arguments": '{"path": "a.py"}'}}]}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    })
    client = _install(spy)
    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "read a.py"}],
    }).json()

    assert [b["type"] for b in body["content"]] == ["text", "tool_use"]
    block = body["content"][1]
    assert block["name"] == "read_file"
    assert block["input"] == {"path": "a.py"}
    assert block["id"] == "call_1"
    assert body["stop_reason"] == "tool_use"

    from claudecode import messages_response as mr
    broken = mr.response_object({
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ls", "arguments": "{not json"}}]}}],
    }, "m", "m", "spy")
    assert broken["content"][0]["input"] == {}
    assert broken["content"][0]["name"] == "ls"


def test_unit_claude_a_reasoning_only_answer_is_not_an_empty_turn():
    """nemotron streams its whole answer as reasoning; the turn must not be empty.

    An empty content list reads to an agent as a refusal or a failure. The
    reasoning becomes ordinary text instead -- deliberately *not* a ``thinking``
    block, because a real one carries a signature no free model can produce and a
    client would be entitled to reject an unsigned one.
    """
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "stop", "message": {
            "content": "", "reasoning_content": "2+2 is 4"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 9},
    })
    client = _install(spy)
    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "2+2?"}],
    }).json()

    assert body["content"], "a reasoning-only answer produced an empty turn"
    assert body["content"][0]["type"] == "text"
    assert "4" in body["content"][0]["text"]
    assert not any(b["type"] == "thinking" for b in body["content"])


# ---------------------------------------------------------------------------
# 4. Streaming
# ---------------------------------------------------------------------------
def _events(raw: str):
    """The ``event:`` names, in order, from an SSE body."""
    return [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]


def test_unit_claude_stream_emits_the_full_anthropic_lifecycle():
    """Anthropic's event order is a contract, not a suggestion.

    A client is entitled to assume a block is opened before it is written to and
    closed before the next opens, and that ``message_delta`` carries the
    stop_reason and usage. OpenAI's format has none of that structure, so all of
    it is synthesised here -- and a missing event leaves a client waiting.
    """
    spy = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"content":"2 plus 2 "}}]}',
        b'data: {"choices":[{"delta":{"content":"is 4"}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":9,"completion_tokens":4}}',
        b"data: [DONE]",
    ])
    client = _install(spy)

    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "2+2?"}],
        "stream": True,
    }) as resp:
        assert resp.status_code == 200
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _events(raw)
    assert events[0] == "message_start", events
    assert events[-1] == "message_stop", events
    assert events[-2] == "message_delta", events
    assert events.count("content_block_start") == events.count("content_block_stop") == 1, events
    # Consecutive text deltas share one block rather than opening a new one each.
    assert events.count("content_block_delta") == 2, events

    # The event: line and the data type must agree; clients read either one.
    for name in set(events):
        assert f'"type":"{name}"' in raw, name

    # stop_reason and usage belong to message_delta -- a chat upstream sends usage
    # in a trailing chunk, and forwarding that shape would leave a client with no
    # token counts at all.
    delta = [ln for ln in raw.splitlines() if '"message_delta"' in ln][0]
    assert '"stop_reason":"end_turn"' in delta
    assert '"input_tokens":9' in delta and '"output_tokens":4' in delta
    assert "2 plus 2 " in raw and "is 4" in raw


def test_unit_claude_streamed_tool_calls_get_their_own_block_each():
    """Two tool calls must not share a block index, and fragments must pass through.

    Anthropic streams tool arguments as ``input_json_delta`` fragments, same as
    OpenAI -- but the enclosing block needs the name and id up front, which OpenAI
    only sends on the first fragment. Reusing one index for two calls is the silent
    version of this bug: the client sees a single call with both sets of arguments
    concatenated into invalid JSON.
    """
    spy = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"read_file","arguments":"{\\"path\\""}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":": \\"a.py\\"}"}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c2",'
        b'"function":{"name":"list_dir","arguments":"{}"}}]}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy)

    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "read a.py"}],
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    starts = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
              if ln.startswith("data: ") and '"content_block_start"' in ln]
    assert len(starts) == 2, starts
    assert [s["index"] for s in starts] == [0, 1], starts
    assert [s["content_block"]["name"] for s in starts] == ["read_file", "list_dir"]
    assert [s["content_block"]["id"] for s in starts] == ["c1", "c2"]
    # input starts empty and is filled by deltas, per Anthropic's own format.
    assert all(s["content_block"]["input"] == {} for s in starts)

    # Argument fragments arrive against the right index, and concatenating them
    # reproduces the original JSON.
    frags = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
             if ln.startswith("data: ") and '"input_json_delta"' in ln]
    first = "".join(f["delta"]["partial_json"] for f in frags if f["index"] == 0)
    assert json.loads(first) == {"path": "a.py"}, first

    assert '"stop_reason":"tool_use"' in raw
    events = _events(raw)
    assert events.count("content_block_start") == events.count("content_block_stop") == 2


def test_unit_claude_stream_reports_a_truncated_turn():
    """A stream that stops without a finish_reason must not be filed as success.

    The ledger would otherwise record a truncated answer as ok_stream, hiding
    exactly the failure the ledger exists to surface. The client still gets a
    well-formed ending, because leaving the lifecycle open would hang it.
    """
    from routing import stream_guard
    from claudecode import messages_stream

    def dies():
        yield b'data: {"choices":[{"delta":{"content":"half an ans"}}]}'

    outcome = stream_guard.StreamOutcome()
    raw = b"".join(messages_stream.stream_events(dies(), MODEL, outcome)).decode("utf-8")

    assert outcome.completed is False, "a truncated stream was reported as complete"
    # Even so the lifecycle closes: an open block and no message_stop would leave
    # the client waiting forever.
    events = _events(raw)
    assert events.count("content_block_start") == events.count("content_block_stop") == 1
    assert events[-1] == "message_stop"

    def finishes():
        yield b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}'

    ok = stream_guard.StreamOutcome()
    list(messages_stream.stream_events(finishes(), MODEL, ok))
    assert ok.completed is True


# ---------------------------------------------------------------------------
# 5. Model mapping and Claude Code's own quirks
# ---------------------------------------------------------------------------
def test_unit_claude_model_ids_resolve_to_a_real_free_model():
    """Claude Code asks for Anthropic ids; none of them exist here.

    A request naming ``claude-sonnet-4-6`` has to become a real free model or it
    404s, which for Claude Code ends the turn. Pinning a real OpenCode id must
    still work untouched, because that is the honest way to choose a model.
    """
    from claudecode import model_map

    spy = SpyProvider()
    client = _install(spy)

    # A real id, and the claude- alias discovery advertises, both land on it.
    assert model_map.resolve(MODEL, app_mod.catalog) == MODEL
    assert model_map.resolve(f"claude-{MODEL}", app_mod.catalog) == MODEL
    assert model_map.resolve(config.MULTIMODEL_ID, app_mod.catalog) == config.MULTIMODEL_ID

    # Size classes route by what the name implies about depth. The spy catalog
    # serves one model, so every class lands there -- what matters is that each is
    # recognised rather than falling through as unknown.
    for asked in ("claude-sonnet-4-6", "claude-opus-4-8", "claude-3-5-haiku-20241022"):
        assert model_map.size_class(asked) is not None, asked
        r = client.post("/v1/messages", json={
            "model": asked, "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200, (asked, r.status_code)
        # The response echoes what the client asked for, not what ran.
        assert r.json()["model"] == asked
        assert r.json()["lingling"]["routed_model"] == MODEL

    # Something with no recognisable class goes to the dispatcher, the same answer
    # any other client gets for an unknown model.
    assert model_map.resolve("mistral-large", app_mod.catalog) == config.MULTIMODEL_ID


def test_unit_claude_background_haiku_call_is_served():
    """Claude Code makes small-fast-model calls for titles and summaries.

    Those go to whatever id resolves for its ``haiku`` alias, and there is no
    documented switch to turn them off. An unserved haiku id means background 404s
    for the whole session, so the alias has to resolve like any other.
    """
    spy = SpyProvider()
    client = _install(spy)

    r = client.post("/v1/messages", json={
        "model": "claude-3-5-haiku-20241022", "max_tokens": 32,
        "messages": [{"role": "user", "content": "Summarise this session."}],
    })
    assert r.status_code == 200, r.text[:200]
    assert spy.sent[0]["model"] == MODEL


def test_unit_claude_discovery_list_only_offers_ids_claude_code_accepts():
    """Claude Code drops any discovered model whose id lacks a claude prefix.

    So the free models are advertised under ``claude-`` aliases. Without the
    prefix the picker silently shows nothing, which looks like a broken gateway
    rather than a naming rule.
    """
    from claudecode import model_map

    spy = SpyProvider()
    _install(spy)
    listed = model_map.advertised_models(app_mod.catalog)

    assert listed, "discovery would show an empty model list"
    for entry in listed:
        assert entry["id"].startswith("claude") or entry["id"].startswith("anthropic"), entry
        assert entry["type"] == "model"
        assert entry["display_name"]

    # Every advertised id must resolve back to something routable, or the picker
    # offers models that fail when chosen.
    for entry in listed:
        resolved = model_map.resolve(entry["id"], app_mod.catalog)
        assert resolved == MODEL or resolved == config.MULTIMODEL_ID, (entry, resolved)


def test_unit_claude_rejects_a_body_that_is_not_a_request():
    """Well-formed JSON of the wrong shape must answer 400, not 500.

    Anthropic requires model and messages. Reading those fields off a list or a
    string raises inside the handler and surfaces as a raw 500 with no ledger row,
    which tells the client nothing about what it got wrong.
    """
    spy = SpyProvider()
    client = _install(spy)

    for body in (
        ["not", "an", "object"],
        "a bare string",
        42,
        {"messages": [{"role": "user", "content": "hi"}]},          # no model
        {"model": MODEL},                                            # no messages
        {"model": MODEL, "messages": []},
        {"model": MODEL, "messages": ["a string, not a turn"]},
        {"model": 42, "messages": [{"role": "user", "content": "hi"}]},
    ):
        r = client.post("/v1/messages", json=body)
        assert r.status_code == 400, f"{str(body)[:60]} -> {r.status_code}"


def test_unit_claude_accepts_a_system_turn_inside_messages():
    """Claude Code sends `role: system` turns; refusing them kills the session.

    Anthropic's Messages API has no system *role* for the top-level prompt -- that
    is the separate ``system`` field -- so an early draft rejected the role
    outright. But Claude Code v2.1.x also sends mid-conversation system messages,
    which are real traffic, and the rejection failed the very first "hi" of a
    session with `400 Unsupported message role: 'system'`.

    An unrecognised role is likewise forwarded rather than refused: a future
    Anthropic role should degrade to a readable turn, not take a session down.
    """
    spy = SpyProvider()
    client = _install(spy)

    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "system": "You are a coding agent.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "system", "content": "The user switched to /effort max."},
            {"role": "user", "content": "carry on"},
        ],
    })
    assert r.status_code == 200, r.text[:300]

    roles = [m["role"] for m in spy.sent[0]["messages"]]
    # The top-level prompt is still hoisted first, and the mid-conversation system
    # turn keeps its position in the history rather than being merged or dropped.
    assert roles == ["system", "user", "assistant", "system", "user"], roles
    assert spy.sent[0]["messages"][3]["content"] == "The user switched to /effort max."

    # The block-list form of a system turn survives too.
    spy.sent.clear()
    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "be terse"}]},
            {"role": "user", "content": "hi"},
        ],
    })
    assert r.status_code == 200, r.text[:300]
    assert spy.sent[0]["messages"][0] == {"role": "system", "content": "be terse"}

    # An unknown role is carried as user content instead of failing the request.
    spy.sent.clear()
    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "future_role", "content": "still readable"}],
    })
    assert r.status_code == 200, r.text[:300]
    assert spy.sent[0]["messages"][0] == {"role": "user", "content": "still readable"}


# ---------------------------------------------------------------------------
# 6. The setup helper that writes Claude Code's settings.json
# ---------------------------------------------------------------------------
def test_unit_claude_setup_writes_a_config_that_actually_wins():
    """The setup helper must beat every leftover that outranks a shell variable.

    A machine that has been through other gateways ends up with an undocumented
    ``provider`` block in settings.json and persisted ANTHROPIC_* variables. That
    provider block is what makes "I set the variables and nothing changed" happen,
    so it must be removed -- and the variables must be written where Anthropic
    documents settings-file values as beating a shell export: the ``env`` key.
    """
    import tempfile as tf

    from claudecode import setup_gui

    home = Path(tf.mkdtemp(prefix="cc-setup-"))
    saved_dir, saved_file = setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE
    setup_gui.SETTINGS_DIR = home / ".claude"
    setup_gui.SETTINGS_FILE = setup_gui.SETTINGS_DIR / "settings.json"
    try:
        # Exactly the shape a third-party launcher leaves behind, alongside real
        # settings the user cares about.
        setup_gui.SETTINGS_DIR.mkdir(parents=True)
        setup_gui.SETTINGS_FILE.write_text(json.dumps({
            "model": "sonnet",
            "effortLevel": "xhigh",
            "theme": "dark",
            "permissions": {"allow": ["Bash(npm run test *)"]},
            "provider": {"type": "custom", "name": "blaze",
                         "apiUrl": "http://127.0.0.1:3000",
                         "apiKey": "redacted-in-test",
                         "model": "deepseek-v4-pro"},
        }), encoding="utf-8")

        result = setup_gui.apply("http://127.0.0.1:8000/", MODEL)
        written = json.loads(setup_gui.SETTINGS_FILE.read_text(encoding="utf-8"))

        # The block that was overriding everything is gone, and reported.
        assert "provider" not in written, written
        assert any("provider" in c for c in result["conflicts"]), result["conflicts"]

        # Routing goes in `env`, which the docs say beats a shell export.
        env = written["env"]
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000", env
        # The gateway is keyless, so the credential slot is a local sentinel:
        # non-empty (Claude Code refuses to send with an empty token) but
        # never validated by the gateway, and it neutralises any leftover
        # shell export from another gateway.
        assert env["ANTHROPIC_AUTH_TOKEN"] == setup_gui.LOCAL_AUTH_TOKEN

        # `model` is a starting default only.
        assert written["model"] == MODEL

        # Settings the user cares about survive untouched.
        assert written["theme"] == "dark"
        assert written["permissions"]["allow"] == ["Bash(npm run test *)"]

        # The old file is recoverable, because it held their permissions.
        assert result["backup"] and Path(result["backup"]).exists()
        recovered = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
        assert recovered["provider"]["name"] == "blaze"

        # Running it twice must not accumulate anything or lose the choice.
        setup_gui.apply("http://127.0.0.1:8000", MODEL)
        twice = json.loads(setup_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert twice == written, "apply is not idempotent"
    finally:
        setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE = saved_dir, saved_file


def test_unit_claude_setup_leaves_model_and_effort_to_the_terminal():
    """Setup must not pin what ``/model`` and ``/effort`` are supposed to change.

    Anthropic's precedence rule is explicit: ``ANTHROPIC_MODEL`` overrides the
    ``model`` setting, and "Claude Code uses the settings key only when the
    environment variable is unset". Writing it into ``env`` therefore makes
    ``/model`` a no-op -- the picker shows the new choice while every request
    carries the pinned one. ``CLAUDE_CODE_EFFORT_LEVEL`` does the same to
    ``/effort``.

    Depth is read off each individual request by ``claudecode.effort`` and clamped
    per routed model, exactly as for Codex, so a setup-time value would replace a
    live dial with a dead one.
    """
    import tempfile as tf

    from claudecode import setup_gui

    home = Path(tf.mkdtemp(prefix="cc-dynamic-"))
    saved_dir, saved_file = setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE
    setup_gui.SETTINGS_DIR = home / ".claude"
    setup_gui.SETTINGS_FILE = setup_gui.SETTINGS_DIR / "settings.json"
    try:
        # A file already carrying both pins, as an earlier draft of this wrote.
        setup_gui.SETTINGS_DIR.mkdir(parents=True)
        setup_gui.SETTINGS_FILE.write_text(json.dumps({
            "model": "old-model",
            "effortLevel": "max",
            "env": {"ANTHROPIC_MODEL": "old-model",
                    "CLAUDE_CODE_EFFORT_LEVEL": "max",
                    "SOMETHING_ELSE": "keep me"},
        }), encoding="utf-8")

        setup_gui.apply("http://127.0.0.1:8000", MODEL)
        written = json.loads(setup_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
        env = written["env"]

        # Neither hijacking variable may survive.
        assert "ANTHROPIC_MODEL" not in env, env
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env, env
        assert "effortLevel" not in written, written

        # Only endpoint and credential are managed; unrelated vars are preserved.
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
        assert env["ANTHROPIC_AUTH_TOKEN"] == setup_gui.LOCAL_AUTH_TOKEN
        assert env["SOMETHING_ELSE"] == "keep me"

        # `model` is documented and overridable by /model, so it is safe to write.
        assert written["model"] == MODEL
    finally:
        setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE = saved_dir, saved_file


def test_unit_claude_setup_survives_a_broken_or_missing_settings_file():
    """A first run and a corrupt file must both end with a usable config.

    A fresh machine has no ``~/.claude`` at all. A machine mid-crash can have a
    truncated one. Neither may raise, and a broken file must be reported rather
    than silently discarded -- it may still be readable by hand.
    """
    import tempfile as tf

    from claudecode import setup_gui

    saved_dir, saved_file = setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE
    try:
        # First run: nothing exists yet, not even the directory.
        home = Path(tf.mkdtemp(prefix="cc-fresh-"))
        setup_gui.SETTINGS_DIR = home / ".claude"
        setup_gui.SETTINGS_FILE = setup_gui.SETTINGS_DIR / "settings.json"
        result = setup_gui.apply("http://127.0.0.1:8000", MODEL)
        written = json.loads(setup_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert result["backup"] is None
        assert written["model"] == MODEL
        # A fixed non-empty local sentinel fills the credential slot: Claude
        # Code treats a present-but-empty token as "not logged in" and refuses
        # to send, while a fixed value keeps apply idempotent and still
        # neutralises a leftover shell export from another gateway.
        assert written["env"]["ANTHROPIC_AUTH_TOKEN"] == setup_gui.LOCAL_AUTH_TOKEN
        assert written["$schema"].endswith("claude-code-settings.json")

        # Corrupt file: reported, still fixed, original preserved.
        home = Path(tf.mkdtemp(prefix="cc-broken-"))
        setup_gui.SETTINGS_DIR = home / ".claude"
        setup_gui.SETTINGS_FILE = setup_gui.SETTINGS_DIR / "settings.json"
        setup_gui.SETTINGS_DIR.mkdir(parents=True)
        setup_gui.SETTINGS_FILE.write_text('{"model": "sonnet", trunc', encoding="utf-8")
        result = setup_gui.apply("http://127.0.0.1:8000", MODEL)
        assert result["warning"] and "valid JSON" in result["warning"]
        assert result["backup"] and Path(result["backup"]).exists()
        written = json.loads(setup_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert written["model"] == MODEL
    finally:
        setup_gui.SETTINGS_DIR, setup_gui.SETTINGS_FILE = saved_dir, saved_file


def test_unit_claude_setup_only_offers_models_the_gateway_serves():
    """The dropdown is populated live, so it cannot offer a model that 404s.

    Hardcoding the free-model list would go stale the moment OpenCode changes its
    tier, and the failure would be a user picking a model that does not exist.
    """
    from claudecode import setup_gui

    spy = SpyProvider()
    client = _install(spy)

    ids = [entry["id"] for entry in client.get("/v1/models").json()["data"]]
    assert MODEL in ids, ids
    assert config.MULTIMODEL_ID in ids, "the auto router should be selectable too"

    # An unreachable gateway is an explained failure, not a traceback: the window
    # tells the user to start the backend.
    models, error = setup_gui.fetch_models("http://127.0.0.1:9", timeout=1.0)
    assert models == []
    assert error and "backend running" in error


# ---------------------------------------------------------------------------
# 6. LIVE -- the free tier must actually honour the translated dial
# ---------------------------------------------------------------------------
def test_live_claude_effort_changes_what_the_model_spends():
    """The dial must change the model's behaviour, not merely reach it.

    The hermetic tests prove the right value arrives at the provider. They cannot
    prove OpenCode *honours* it: OpenCode returns 200 for an effort value a model
    does not implement, so an ignored dial and a working one look identical from
    inside. The only way to tell them apart is to send one prompt at two rungs
    against the real tier and compare what came back.

    Reasoning tokens are the signal -- a model thinking harder spends more of
    them. Free models are noisy, so this asserts only that the two runs differ,
    not by how much.
    """
    # The module-level app is already wired to the real OpenCode provider; the
    # autouse ``_catalog_isolation`` fixture guarantees no spy catalog from a
    # hermetic test is installed. (An earlier version reloaded the module here
    # to dodge spy stubs -- but reload re-executes app.py, which builds a NEW
    # UnifiedCatalog, and the fixture then binds the ORIGINAL catalog's methods
    # onto the new instance. Every later test that mutated ``_logical`` and
    # checked ``is_burned()`` read two different objects and saw the burn
    # vanish. The fixture already restores the real catalog, so the reload is
    # both unnecessary and harmful.)
    live = TestClient(app_mod.app)

    app_mod.catalog.refresh(force=True)
    dialled = [
        (m.id, (getattr(m, "capabilities", None) or {}).get("effort") or [])
        for m in app_mod.catalog.free()
    ]
    dialled = [(mid, values) for mid, values in dialled if len(values) >= 2]
    if not dialled:
        raise SkipTest("no free model publishes two or more effort values today")

    model_id, values = dialled[0]
    prompt = ("A farmer has 17 sheep and all but 9 run away. He then buys twice as "
              "many as he has left, and sells 5. How many does he have? "
              "Answer with the number only.")

    spend = {}
    for label in ("low", "max"):
        r = live.post("/v1/messages", json={
            "model": model_id, "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": label},
        })
        if r.status_code != 200:
            raise SkipTest(f"free tier unavailable: HTTP {r.status_code}")
        body = r.json()
        # The response must be a real Anthropic message even on the live path.
        assert body["type"] == "message" and body["role"] == "assistant"
        assert body["content"], "the live model produced an empty turn"
        # A thinking model puts its reasoning first, in its own block; the answer
        # follows as text. Both are legal, and every block must be a known type.
        kinds = [block["type"] for block in body["content"]]
        assert set(kinds) <= {"thinking", "text", "tool_use"}, kinds
        assert "text" in kinds, f"no visible answer, only {kinds}"
        # Reasoning must never be concatenated into the answer -- that renders the
        # model's private deliberation as its reply.
        thinking = [b["thinking"] for b in body["content"] if b["type"] == "thinking"]
        answer = [b["text"] for b in body["content"] if b["type"] == "text"]
        for thought in thinking:
            assert thought not in answer[0], "reasoning leaked into the answer text"
        assert body["stop_reason"] in ("end_turn", "max_tokens", "tool_use", "refusal")
        spend[label] = body["usage"]["output_tokens"]

    assert spend["low"] > 0 and spend["max"] > 0, spend
    assert spend["low"] != spend["max"], (
        f"{model_id} spent {spend['low']} tokens at both rungs -- the dial may be "
        f"ignored, or the prompt is too easy to show a difference"
    )


def test_live_claude_streaming_turn_against_the_real_tier():
    """One real streamed turn, to prove the event lifecycle survives a real model.

    Hermetic streaming tests use canned frames in the exact shape OpenCode is
    expected to send. This one takes whatever the live tier actually sends -- odd
    chunk boundaries, reasoning fields, an empty first delta -- and checks the
    translated lifecycle still holds together.
    """
    # Same reasoning as the effort live test above: the module-level app is
    # already on the real provider, the fixture restores the real catalog, and
    # reloading the module would orphan the catalog methods every later test
    # relies on.
    live = TestClient(app_mod.app)

    app_mod.catalog.refresh(force=True)
    free = app_mod.catalog.free()
    if not free:
        raise SkipTest("the free catalog is empty")

    with live.stream("POST", "/v1/messages", json={
        "model": free[0].id, "max_tokens": 200,
        "messages": [{"role": "user", "content": "Say the single word: ready"}],
        "stream": True,
    }) as resp:
        if resp.status_code != 200:
            raise SkipTest(f"free tier unavailable: HTTP {resp.status_code}")
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _events(raw)
    assert events[0] == "message_start", events[:3]
    assert events[-1] == "message_stop", events[-3:]
    assert "message_delta" in events
    assert events.count("content_block_start") == events.count("content_block_stop"), events
    assert events.count("content_block_delta") > 0, "the live model sent no content"
    # Every frame must be parseable JSON: a client parses each one as it arrives.
    for line in raw.splitlines():
        if line.startswith("data: "):
            json.loads(line[len("data: "):])




def test_unit_claude_reasoning_never_reaches_a_client_that_did_not_ask():
    """Unrequested reasoning must not appear in the response at all.

    Two wrong answers were tried before this one. Folding reasoning into the
    answer's text block ran them together on screen -- "...No need for
    tools.Hi! How can I help you today?" -- so the model's deliberation read as
    its reply. Giving it its own thinking block was worse: Anthropic's docs say
    thinking is "redacted by the API and shown as a collapsed stub", and Claude
    Code relies on that redaction rather than hiding anything itself. Lingling has
    no signature and no encryption, so an unrequested block gets rendered in full
    and a long think buries the answer under pages of scratch work.

    The client did not ask for thinking, so it does not get any.
    """
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "stop", "message": {
            "content": "Hi! How can I help you today?",
            "reasoning_content": 'The user just said "hi". No need for tools.'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 12},
    })
    client = _install(spy)

    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }).json()

    assert [b["type"] for b in body["content"]] == ["text"], body["content"]
    assert body["content"][0]["text"] == "Hi! How can I help you today?"
    # The exact symptom, in both directions: no deliberation in the reply, and no
    # thinking block smuggled alongside it.
    assert "No need for tools" not in json.dumps(body)

    # Streaming: the reasoning is consumed and dropped, not turned into frames.
    spy2 = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"reasoning_content":"The user said hi. "}}]}',
        b'data: {"choices":[{"delta":{"reasoning_content":"No tools needed."}}]}',
        b'data: {"choices":[{"delta":{"content":"Hi! "}}]}',
        b'data: {"choices":[{"delta":{"content":"How can I help?"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy2)
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    starts = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
              if ln.startswith("data: ") and '"content_block_start"' in ln]
    assert [s["content_block"]["type"] for s in starts] == ["text"], starts
    assert "No tools needed" not in raw, "reasoning leaked into the stream"
    assert '"type":"thinking_delta"' not in raw
    # The answer still arrives intact, and the lifecycle stays well-formed.
    assert '"text":"Hi! "' in raw
    events = _events(raw)
    assert events.count("content_block_start") == events.count("content_block_stop") == 1
    assert events[-1] == "message_stop"


def test_unit_claude_reasoning_is_shown_when_thinking_is_requested():
    """A client that asks for thinking gets it, in its own block.

    ``thinking: {"type": "enabled"|"adaptive"}`` is the opt-in. Then the reasoning
    is legitimate output and belongs in a ``thinking`` block -- never concatenated
    into the answer, which is what made the two run together on screen.
    """
    for asked in ({"type": "enabled", "budget_tokens": 4000}, {"type": "adaptive"}):
        spy = SpyProvider(answer={
            "choices": [{"finish_reason": "stop", "message": {
                "content": "The answer is 4.",
                "reasoning_content": "2+2 is 4."}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8},
        })
        client = _install(spy)
        body = client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 64,
            "messages": [{"role": "user", "content": "2+2?"}],
            "thinking": asked,
        }).json()

        kinds = [b["type"] for b in body["content"]]
        assert kinds == ["thinking", "text"], (asked, kinds)
        assert body["content"][0]["thinking"] == "2+2 is 4."
        assert body["content"][1]["text"] == "The answer is 4."
        # Separate blocks, so the deliberation is not inside the reply.
        assert "2+2 is 4." not in body["content"][1]["text"]

    # Streaming, with thinking asked for: two blocks, Anthropic's own delta types.
    spy2 = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"reasoning_content":"Adding two and two. "}}]}',
        b'data: {"choices":[{"delta":{"content":"4"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy2)
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "2+2?"}],
        "thinking": {"type": "adaptive"},
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    starts = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
              if ln.startswith("data: ") and '"content_block_start"' in ln]
    assert [s["content_block"]["type"] for s in starts] == ["thinking", "text"], starts
    assert [s["index"] for s in starts] == [0, 1], starts
    assert '"type":"thinking_delta"' in raw
    assert '"thinking":"Adding two and two. "' in raw
    # Reasoning must not also arrive as a text delta.
    text_deltas = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
                   if ln.startswith("data: ") and '"text_delta"' in ln]
    assert "Adding two and two" not in "".join(d["delta"]["text"] for d in text_deltas)


def test_unit_claude_a_reasoning_only_turn_is_reported_not_dumped():
    """A model that never answers must be reported, not have its scratch work shown.

    Measured against the real free tier: on an agentic prompt, deepseek at high
    effort sent 1203 frames carrying 5004 characters of ``reasoning_content`` with
    an **empty** ``content`` on every single one. An earlier version promoted that
    reasoning to the answer -- on the theory that an empty turn reads as a refusal
    -- and buried the terminal in five thousand characters of deliberation.

    So the turn ends with a short factual note and ``stop_reason: max_tokens``,
    which is what a real Anthropic endpoint reports when the budget ran out. An
    agent can act on that; it cannot act on a wall of thinking.
    """
    from routing import stream_guard
    from claudecode import messages_stream

    def only_reasons():
        for _ in range(40):
            yield (b'data: {"choices":[{"delta":{"content":"",'
                   b'"reasoning_content":"Let me think about this. "}}]}')

    outcome = stream_guard.StreamOutcome()
    raw = b"".join(
        messages_stream.stream_events(only_reasons(), MODEL, outcome)
    ).decode("utf-8")

    text = "".join(
        json.loads(ln[len("data: "):])["delta"]["text"]
        for ln in raw.splitlines()
        if ln.startswith("data: ") and '"text_delta"' in ln
    )
    # A visible turn, but not the reasoning itself.
    assert text, "the client got nothing at all"
    assert "Let me think about this" not in text, f"reasoning was dumped: {text[:120]}"
    assert MODEL in text and "without producing an answer" in text, text
    # Long enough to be a real complaint, short enough not to be a flood.
    assert len(text) < 300, f"the note itself is too long: {len(text)}"
    # And no thinking block, because this client did not ask for one.
    assert '"type":"thinking"' not in raw

    # stop_reason must say the budget ran out, so an agent can retry rather than
    # treating a silent failure as a finished turn.
    delta = [ln for ln in raw.splitlines() if '"message_delta"' in ln][0]
    assert '"stop_reason":"max_tokens"' in delta, delta

    # Non-streaming path reports the same way.
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "stop", "message": {
            "content": "", "reasoning_content": "Thinking at length. " * 50}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 900},
    })
    client = _install(spy)
    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "build me a website"}],
    }).json()

    assert [b["type"] for b in body["content"]] == ["text"], body["content"]
    assert "Thinking at length" not in body["content"][0]["text"]
    assert "without producing an answer" in body["content"][0]["text"]
    assert body["stop_reason"] == "max_tokens", body["stop_reason"]

    # A turn that produced a tool call but no prose is *not* a failed turn: the
    # agent has something to run, and the note would be noise.
    spy2 = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"content":"","reasoning_content":"I should read it. "}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"Read","arguments":"{}"}}]}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy2)
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "read a.py"}],
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "without producing an answer" not in raw, "a tool call is a real answer"
    assert '"stop_reason":"tool_use"' in raw


def test_unit_claude_reasoning_never_reaches_a_client_that_did_not_ask():
    """Unrequested reasoning must not appear in the response at all.

    Two wrong answers were tried before this one. Folding reasoning into the
    answer's text block ran them together on screen -- "...No need for
    tools.Hi! How can I help you today?" -- so the model's deliberation read as
    its reply. Giving it its own thinking block was worse: Anthropic's docs say
    thinking is "redacted by the API and shown as a collapsed stub", and Claude
    Code relies on that redaction rather than hiding anything itself. Lingling has
    no signature and no encryption, so an unrequested block gets rendered in full
    and a long think buries the answer under pages of scratch work.

    The client did not ask for thinking, so it does not get any.
    """
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "stop", "message": {
            "content": "Hi! How can I help you today?",
            "reasoning_content": 'The user just said "hi". No need for tools.'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 12},
    })
    client = _install(spy)

    body = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }).json()

    assert [b["type"] for b in body["content"]] == ["text"], body["content"]
    assert body["content"][0]["text"] == "Hi! How can I help you today?"
    # The exact symptom, in both directions: no deliberation in the reply, and no
    # thinking block smuggled alongside it.
    assert "No need for tools" not in json.dumps(body)

    # Streaming: the reasoning is consumed and dropped, not turned into frames.
    spy2 = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"reasoning_content":"The user said hi. "}}]}',
        b'data: {"choices":[{"delta":{"reasoning_content":"No tools needed."}}]}',
        b'data: {"choices":[{"delta":{"content":"Hi! "}}]}',
        b'data: {"choices":[{"delta":{"content":"How can I help?"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy2)
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    starts = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
              if ln.startswith("data: ") and '"content_block_start"' in ln]
    assert [s["content_block"]["type"] for s in starts] == ["text"], starts
    assert "No tools needed" not in raw, "reasoning leaked into the stream"
    assert '"type":"thinking_delta"' not in raw
    # The answer still arrives intact, and the lifecycle stays well-formed.
    assert '"text":"Hi! "' in raw
    events = _events(raw)
    assert events.count("content_block_start") == events.count("content_block_stop") == 1
    assert events[-1] == "message_stop"


def test_unit_claude_reasoning_is_shown_when_thinking_is_requested():
    """A client that asks for thinking gets it, in its own block.

    ``thinking: {"type": "enabled"|"adaptive"}`` is the opt-in. Then the reasoning
    is legitimate output and belongs in a ``thinking`` block -- never concatenated
    into the answer, which is what made the two run together on screen.
    """
    for asked in ({"type": "enabled", "budget_tokens": 4000}, {"type": "adaptive"}):
        spy = SpyProvider(answer={
            "choices": [{"finish_reason": "stop", "message": {
                "content": "The answer is 4.",
                "reasoning_content": "2+2 is 4."}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8},
        })
        client = _install(spy)
        body = client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 64,
            "messages": [{"role": "user", "content": "2+2?"}],
            "thinking": asked,
        }).json()

        kinds = [b["type"] for b in body["content"]]
        assert kinds == ["thinking", "text"], (asked, kinds)
        assert body["content"][0]["thinking"] == "2+2 is 4."
        assert body["content"][1]["text"] == "The answer is 4."
        # Separate blocks, so the deliberation is not inside the reply.
        assert "2+2 is 4." not in body["content"][1]["text"]

    # Streaming, with thinking asked for: two blocks, Anthropic's own delta types.
    spy2 = SpyProvider(stream_frames=[
        b'data: {"choices":[{"delta":{"reasoning_content":"Adding two and two. "}}]}',
        b'data: {"choices":[{"delta":{"content":"4"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    client = _install(spy2)
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "2+2?"}],
        "thinking": {"type": "adaptive"},
        "stream": True,
    }) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    starts = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
              if ln.startswith("data: ") and '"content_block_start"' in ln]
    assert [s["content_block"]["type"] for s in starts] == ["thinking", "text"], starts
    assert [s["index"] for s in starts] == [0, 1], starts
    assert '"type":"thinking_delta"' in raw
    assert '"thinking":"Adding two and two. "' in raw
    # Reasoning must not also arrive as a text delta.
    text_deltas = [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
                   if ln.startswith("data: ") and '"text_delta"' in ln]
    assert "Adding two and two" not in "".join(d["delta"]["text"] for d in text_deltas)


def test_unit_claude_stream_loses_no_byte_of_the_answer():
    """Every character an upstream sends must reach the client unchanged.

    Real output arrived looking truncated mid-expression --
    ``const clamp=(v,a,b)=>Math.min(b,Math.max(a`` -- which is either the model
    streaming sloppily or this translator dropping characters at frame boundaries.
    The second would silently corrupt every code block Claude Code writes, so it
    has to be ruled out with bytes rather than reasoning.

    Fragment sizes go down to one character, because a provider splits mid-token
    and mid-escape-sequence, and the payload carries every character class that
    could break a JSON round-trip.
    """
    source = (
        "const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));\n"
        "const ease=t=>1-Math.pow(1-t,3);\n"
        'el.style.transform=`translateY(${(1-pp)*6}vh)`;\n'
        'const s="a \\"quoted\\" string with \\\\ backslash";\n'
        "\tif (x) { y(); } else { z(); }\n"
        "// dash \u2014 emoji \U0001f600 quote \u201cthis\u201d\n"
    )

    def rebuild(raw: str) -> str:
        """The answer text as a client would reassemble it from the SSE."""
        out = []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            obj = json.loads(line[len("data: "):])
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    out.append(delta["text"])
        return "".join(out)

    for size in (1, 3, 13, 4096):
        fragments = [source[i:i + size] for i in range(0, len(source), size)]
        frames = [
            f'data: {json.dumps({"choices": [{"delta": {"content": f}}]})}'.encode("utf-8")
            for f in fragments
        ]
        frames.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        frames.append(b"data: [DONE]")

        spy = SpyProvider(stream_frames=frames)
        client = _install(spy)
        with client.stream("POST", "/v1/messages", json={
            "model": MODEL, "max_tokens": 2000,
            "messages": [{"role": "user", "content": "write it"}],
            "stream": True,
        }) as resp:
            raw = b"".join(resp.iter_bytes()).decode("utf-8")

        got = rebuild(raw)
        assert got == source, (
            f"{len(fragments)} frames of {size} char(s) corrupted the answer; "
            f"first diff at {next((i for i, (a, b) in enumerate(zip(source, got)) if a != b), len(got))}"
        )
        # Each frame must also stand alone as JSON: a client parses them one by one.
        for line in raw.splitlines():
            if line.startswith("data: "):
                json.loads(line[len("data: "):])


def test_unit_claude_a_truncated_upstream_is_forwarded_verbatim():
    """A model that stops mid-expression must not be padded, trimmed, or repaired.

    This is the other half of the fidelity question: when output really is cut
    short, Lingling forwards exactly what arrived and still closes the event
    lifecycle, so the client renders the partial answer instead of hanging.
    """
    from routing import stream_guard
    from claudecode import messages_stream

    partial = "const clamp=(v,a,b)=>Math.min(b,Math.max(a"

    def dies():
        payload = json.dumps({"choices": [{"delta": {"content": partial}}]})
        yield f"data: {payload}".encode("utf-8")
        # No finish_reason and no [DONE]: the upstream simply stopped.

    outcome = stream_guard.StreamOutcome()
    raw = b"".join(messages_stream.stream_events(dies(), MODEL, outcome)).decode("utf-8")

    text = "".join(
        json.loads(ln[len("data: "):])["delta"]["text"]
        for ln in raw.splitlines()
        if ln.startswith("data: ") and '"text_delta"' in ln
    )
    assert text == partial, f"the partial answer was altered: {text!r}"
    # Filed as broken, but still terminated so the client is not left waiting.
    assert outcome.completed is False
    events = _events(raw)
    assert events[-1] == "message_stop"
    assert events.count("content_block_start") == events.count("content_block_stop")



def test_unit_claude_forced_tool_choice_is_downgraded_not_forwarded():
    """Anthropic's forced tool_choice must become ``auto``, not a 400.

    Measured against the real free tier: chat completions' ``"required"`` and the
    named-function form are both rejected with
    ``400 Thinking mode does not support this tool_choice``, and every free model
    here reasons. Forwarding Anthropic's ``any``/``tool`` therefore turned a
    perfectly good agentic request into a hard failure, which ends Claude Code's
    turn -- and Claude Code sends ``any`` whenever it wants a tool used.

    Anthropic documents the same incompatibility from its own side ("Forced tool
    use is incompatible with manual extended thinking"), so downgrading matches a
    real endpoint rather than hiding a quirk.
    """
    from claudecode import messages_bridge as mb

    assert mb._tool_choice_to_chat({"type": "auto"}) == "auto"
    assert mb._tool_choice_to_chat({"type": "any"}) == "auto"
    assert mb._tool_choice_to_chat({"type": "tool", "name": "Read"}) == "auto"
    assert mb._tool_choice_to_chat({"type": "none"}) == "none"
    assert mb._tool_choice_to_chat({"type": "something_new"}) is None
    assert mb._tool_choice_to_chat("auto") is None

    # Nothing the upstream rejects may ever appear in the outgoing params.
    spy = SpyProvider(answer={
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "Read", "arguments": '{"file_path": "a.py"}'}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    })
    client = _install(spy)

    tools = [{"name": "Read", "description": "Reads a file",
              "input_schema": {"type": "object",
                               "properties": {"file_path": {"type": "string"}}}}]
    for forced in ({"type": "any"}, {"type": "tool", "name": "Read"}):
        spy.sent.clear()
        r = client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 300,
            "messages": [{"role": "user", "content": "read a.py"}],
            "tools": tools, "tool_choice": forced,
        })
        assert r.status_code == 200, (forced, r.status_code, r.text[:200])
        sent_choice = spy.sent[0]["params"].get("tool_choice")
        assert sent_choice == "auto", f"{forced} reached the provider as {sent_choice!r}"
        # The turn still works: a tool call comes back and is reported as one.
        body = r.json()
        assert any(b["type"] == "tool_use" for b in body["content"]), body["content"]
        assert body["stop_reason"] == "tool_use"



def test_unit_claude_thinking_is_replayed_so_the_agent_loop_survives():
    """A tool-calling assistant turn must carry ``reasoning_content`` back.

    This is the bug that killed every agentic session at the second turn. The
    first tool call worked; replaying it to get the model's next step was rejected
    with

        400 The `reasoning_content` in the thinking mode must be passed back
            to the API.

    so the loop ended with an empty answer. An earlier version dropped incoming
    ``thinking`` blocks on the theory that no OpenCode model would want them --
    measured against the real free tier, they are mandatory.

    Two shapes have to work, because Claude Code only sends a thinking block back
    if one was shown to it: the key is replayed when present, and present-but-empty
    otherwise (verified: an empty string satisfies the upstream).
    """
    spy = SpyProvider()
    client = _install(spy)

    tools = [{"name": "Read", "description": "Reads a file",
              "input_schema": {"type": "object",
                               "properties": {"file_path": {"type": "string"}}}}]

    # 1. Claude Code replays the thinking it was shown.
    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 400, "tools": tools,
        "messages": [
            {"role": "user", "content": "What is in config.py?"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "I need to read the file first.",
                 "signature": "sig"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read",
                 "input": {"file_path": "config.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "DEBUG = True"},
            ]},
        ],
    })
    assert r.status_code == 200, r.text[:200]
    assistant = [m for m in spy.sent[0]["messages"] if m["role"] == "assistant"][0]
    assert "reasoning_content" in assistant, assistant
    assert assistant["reasoning_content"] == "I need to read the file first."
    assert assistant["tool_calls"][0]["function"]["name"] == "Read"

    # 2. No thinking block was shown, so none comes back -- but the key must still
    # be there, or the upstream rejects the whole turn.
    spy.sent.clear()
    r = client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 400, "tools": tools,
        "messages": [
            {"role": "user", "content": "What is in config.py?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "Read",
                 "input": {"file_path": "config.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2",
                 "content": "DEBUG = True"},
            ]},
        ],
    })
    assert r.status_code == 200, r.text[:200]
    assistant = [m for m in spy.sent[0]["messages"] if m["role"] == "assistant"][0]
    assert "reasoning_content" in assistant, \
        "a tool-calling turn without the key is rejected by the upstream"
    assert assistant["reasoning_content"] == ""

    # 3. A plain assistant turn needs no such key, and must not gain a spurious one.
    spy.sent.clear()
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 400,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": "bye"},
        ],
    })
    plain = [m for m in spy.sent[0]["messages"] if m["role"] == "assistant"][0]
    assert "reasoning_content" not in plain, plain

    # 4. An assistant turn of pure thinking is no longer discarded: dropping it
    # lost the reasoning the next turn is required to replay.
    spy.sent.clear()
    client.post("/v1/messages", json={
        "model": MODEL, "max_tokens": 400,
        "messages": [
            {"role": "user", "content": "think about it"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Considering.", "signature": "s"}]},
            {"role": "user", "content": "and?"},
        ],
    })
    roles = [m["role"] for m in spy.sent[0]["messages"]]
    assert roles.count("assistant") == 1, roles
    kept = [m for m in spy.sent[0]["messages"] if m["role"] == "assistant"][0]
    assert kept["reasoning_content"] == "Considering."



def test_unit_claude_a_multi_turn_tool_loop_survives_end_to_end():
    """The whole agent loop, replayed the way Claude Code replays it.

    Every individual translation could be right while the loop still dies, which
    is exactly what happened: turn one worked, turn two was rejected because the
    assistant's ``reasoning_content`` was not passed back. This drives three turns
    with tool results fed in between, asserting the shape of each outgoing request.
    """
    calls = []

    class Loop(SpyProvider):
        """Two tool calls, then a final answer -- a real three-step loop."""

        def chat_completions(self, messages, model, secret, timeout=None, **params):
            calls.append([dict(m) for m in messages])
            step = len(calls)
            if step == 1:
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": "", "reasoning_content": "I need config.py first.",
                    "tool_calls": [{"id": "t1", "function": {
                        "name": "Read", "arguments": '{"file_path": "config.py"}'}}]}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 10}}
            if step == 2:
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": "", "reasoning_content": "Now app.py.",
                    "tool_calls": [{"id": "t2", "function": {
                        "name": "Read", "arguments": '{"file_path": "app.py"}'}}]}}],
                    "usage": {"prompt_tokens": 60, "completion_tokens": 10}}
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": "The app uses port 8000.",
                "reasoning_content": "Both files read."}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 8}}

    spy = Loop()
    client = _install(spy)
    tools = [{"name": "Read", "description": "Reads a file",
              "input_schema": {"type": "object",
                               "properties": {"file_path": {"type": "string"}}}}]
    files = {"config.py": "PORT = 8000\n", "app.py": "from config import PORT\n"}

    messages = [{"role": "user", "content": "What port does the app use?"}]
    for turn in range(3):
        r = client.post("/v1/messages", json={
            "model": MODEL, "max_tokens": 500, "tools": tools,
            "thinking": {"type": "adaptive"}, "messages": messages,
        })
        assert r.status_code == 200, (turn, r.status_code, r.text[:200])
        body = r.json()
        # Replay the assistant turn exactly as Claude Code would.
        messages.append({"role": "assistant", "content": body["content"]})
        tool_uses = [b for b in body["content"] if b["type"] == "tool_use"]
        if not tool_uses:
            assert "8000" in "".join(
                b["text"] for b in body["content"] if b["type"] == "text")
            break
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tu["id"],
             "content": files.get(tu["input"].get("file_path", ""), "missing")}
            for tu in tool_uses
        ]})
    else:
        raise AssertionError("the loop never reached a final answer")

    assert len(calls) == 3, f"expected three upstream turns, got {len(calls)}"

    # Every assistant turn carrying tool calls must have replayed its reasoning,
    # or the upstream rejects the request outright.
    for i, sent in enumerate(calls[1:], start=2):
        for msg in sent:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                assert "reasoning_content" in msg, \
                    f"turn {i} would be rejected: {msg}"
        # And the tool result reached the model, in the right position.
        assert any(m["role"] == "tool" for m in sent), f"turn {i} lost the tool result"

    # The final turn saw the whole conversation, in order.
    roles = [m["role"] for m in calls[-1]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"], roles
    assert calls[-1][2]["content"] == "PORT = 8000\n"
    assert calls[-1][4]["content"] == "from config import PORT\n"



def test_unit_a_silent_upstream_is_treated_as_broken_not_waited_on():
    """A stream that stops speaking must be cut off, not waited on indefinitely.

    Measured in a real session: one request sat for **885 seconds** with zero
    tokens before being filed as ``stream_broken``. Neither guard covered it --
    ``STREAM_FIRST_TOKEN_TIMEOUT`` had already been satisfied by the first chunk,
    and ``stream_guard`` only reacts to a stream that *raises*. To the user that is
    a hang.

    httpx's own read timeout does not help: ``stream_chat`` reads with
    ``iter_lines()`` and skips empty lines, and SSE keepalives are empty lines --
    they reset the socket clock while delivering nothing. The watchdog therefore
    lives where usable frames are counted.
    """
    import time as _time

    from routing import stream_idle

    def stalls():
        yield b'data: {"choices":[{"delta":{"content":"half"}}]}'
        # Never yields again, and never raises: the shape that hung.
        _time.sleep(5)
        yield b'data: {"choices":[{"delta":{"content":" more"}}]}'

    started = _time.time()
    got = []
    try:
        for frame in stream_idle.with_idle_timeout(stalls(), budget_s=0.6, log=_QuietLog()):
            got.append(frame)
    except stream_idle.StreamStalled as exc:
        elapsed = _time.time() - started
        assert got, "the frame that did arrive was lost"
        assert b"half" in got[0]
        # Cut off on the budget, not after the upstream's five seconds.
        assert elapsed < 2.5, f"waited {elapsed:.1f}s on a 0.6s budget"
        assert exc.frames == 1, exc.frames
    else:
        raise AssertionError("a silent upstream was not detected")

    # A stream that keeps talking is untouched, however long it runs in total.
    def chatty():
        for i in range(8):
            _time.sleep(0.1)
            yield f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}'.encode()

    frames = list(stream_idle.with_idle_timeout(chatty(), budget_s=0.5, log=_QuietLog()))
    assert len(frames) == 8, f"a healthy stream was cut off after {len(frames)} frames"

    # An upstream error is forwarded unchanged, so existing handling still works.
    def explodes():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
        raise RuntimeError("tunnel dropped")

    try:
        list(stream_idle.with_idle_timeout(explodes(), budget_s=5, log=_QuietLog()))
    except RuntimeError as exc:
        assert "tunnel dropped" in str(exc)
    else:
        raise AssertionError("an upstream error was swallowed")

    # A zero budget disables the watchdog entirely, restoring the old behaviour.
    passthrough = list(stream_idle.with_idle_timeout(
        iter([b"a", b"b"]), budget_s=0, log=_QuietLog()))
    assert passthrough == [b"a", b"b"]


def test_unit_a_stalled_stream_still_ends_the_turn_cleanly():
    """A stall must leave a well-formed response, not an unfinished one.

    Both bridges have to close their blocks and terminate: a client left with an
    open ``content_block`` and no ``message_stop`` waits forever, which is the
    hang the watchdog exists to prevent.
    """
    from routing import stream_guard, stream_idle
    from claudecode import messages_stream

    def stalls():
        yield b'data: {"choices":[{"delta":{"content":"partial ans"}}]}'
        raise stream_idle.StreamStalled(90.0, 1)

    outcome = stream_guard.StreamOutcome()
    raw = b"".join(messages_stream.stream_events(stalls(), MODEL, outcome)).decode("utf-8")

    events = [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]
    assert events[-1] == "message_stop", events[-3:]
    assert events.count("content_block_start") == events.count("content_block_stop")
    # What did arrive is kept, and the turn is not filed as a success.
    assert "partial ans" in raw
    assert outcome.completed is False
    assert outcome.error and "silent" in outcome.error.lower() or outcome.error

    # A stall before anything arrived still explains itself rather than returning
    # an empty turn.
    def stalls_immediately():
        raise stream_idle.StreamStalled(90.0, 0)
        yield b""                                    # pragma: no cover

    outcome2 = stream_guard.StreamOutcome()
    raw2 = b"".join(
        messages_stream.stream_events(stalls_immediately(), MODEL, outcome2)
    ).decode("utf-8")
    text = "".join(
        json.loads(ln[len("data: "):])["delta"]["text"]
        for ln in raw2.splitlines()
        if ln.startswith("data: ") and '"text_delta"' in ln
    )
    assert "stopped responding" in text, text
    assert raw2.rstrip().endswith('{"type":"message_stop"}')
    assert outcome2.completed is False



def test_unit_a_stalled_reader_does_not_leak_forever():
    """The abandoned upstream must be released, and this module must not lie.

    The first version of ``stream_idle`` claimed it closed the source on a stall so
    the httpx connection was released. It could not: the reader thread is blocked
    *inside* that generator, and ``close()`` on a running generator raises
    ``ValueError: generator already executing`` -- which the code then swallowed.
    Measured: the ``finally`` never ran, the connection stayed open, and the reader
    thread was still alive a second later.

    The real contract is narrower and worth pinning: the *consumer* returns
    immediately, and the reader goes away as soon as the upstream's own read
    finishes or times out. Nothing is held for the life of the process.
    """
    import threading as _threading
    import time as _time

    from routing import stream_idle

    released = []

    def upstream():
        # Shaped like providers.base.stream_chat: the finally is what closes the
        # httpx client, and the read eventually fails on the provider's timeout.
        try:
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}'
            _time.sleep(1.5)
            raise RuntimeError("httpx read timeout")
        finally:
            released.append(_time.time())

    started = _time.time()
    try:
        for _ in stream_idle.with_idle_timeout(upstream(), budget_s=0.4, log=_QuietLog()):
            pass
    except stream_idle.StreamStalled:
        consumer_returned = _time.time() - started
    else:
        raise AssertionError("the stall was not detected")

    # The user is not made to wait for the abandoned upstream.
    assert consumer_returned < 1.0, f"consumer waited {consumer_returned:.1f}s"

    # And the reader is not immortal: it ends when its own read does.
    for _ in range(60):
        if not any(t.name == "lingling-stream-reader" for t in _threading.enumerate()):
            break
        _time.sleep(0.1)
    alive = [t.name for t in _threading.enumerate() if t.name == "lingling-stream-reader"]
    assert not alive, "the reader thread outlived the upstream"
    assert released, "the upstream's finally never ran -- the connection leaked"


def test_unit_a_slow_consumer_cannot_be_flooded_by_a_fast_upstream():
    """The queue is bounded, so a fast upstream cannot buffer a whole response.

    Without the bound, a model streaming thousands of frames into a queue nobody
    is draining would hold the entire answer in memory per request.
    """
    import threading as _threading
    import time as _time

    from routing import stream_idle

    produced = []

    def flood():
        for i in range(500):
            produced.append(i)
            yield f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}'.encode()

    gen = stream_idle.with_idle_timeout(flood(), budget_s=5, log=_QuietLog())
    next(gen)                      # start the reader, then stop consuming
    _time.sleep(0.4)
    # The reader is blocked on a full queue rather than having read everything.
    assert len(produced) < 500, f"the reader buffered {len(produced)} frames unbounded"

    gen.close()
    for _ in range(30):
        if not any(t.name == "lingling-stream-reader" for t in _threading.enumerate()):
            break
        _time.sleep(0.1)
    assert not [t for t in _threading.enumerate()
                if t.name == "lingling-stream-reader"], \
        "a reader blocked on a full queue never noticed the consumer had gone"


# ---------------------------------------------------------------------------
# Recycler (Claude Code side) -- pin model_map.resolve's two rules under a
# burn, plus an end-to-end burned-reroute over /v1/messages.
#
# The contract this pins is honest about the leak:
#   * Rule 1: a burned REAL explicit id passes through unchanged, because
#     `catalog.providers_for` is provider-layer bookkeeping and does NOT
#     consult burn state. The messages-handler burned-reroute exists *since*
#     rule 1 -- a fix here would paper over the contract.
#   * Rule 2: an Anthropic alias (`claude-3-5-haiku` etc.) walks its size-class
#     preference list against `catalog.free()`, which is the recycler's burn
#     filter. So the alias routes AROUND a burned first-preference to the
#     next-down one -- the alias-chain side of the "remove from everywhere"
#     guarantee.
# ---------------------------------------------------------------------------
def _cc_recycler_catalog(*entries):
    """Hermetic UnifiedCatalog injected with synthetic LogicalModels (no network).

    Mirrors tests/test_routing.py's burn-bookkeeping helper here so
    test_claudecode cannot reach across files. A synthetic provider returns
    the listed ids on every refresh, so catalog.refresh() materialises fresh
    LogicalModels the same way the live OpenCode fetch does across a dynamic
    /models poll, with burn state preserved from the prior catalog state.

    Each entry is either a bare model id or an ``(id, capabilities)`` tuple;
    the latter attaches the curated provider desc the size-class deriver in
    model_map reads, exactly the way ``providers.opencode.FREE_MODEL_CAPS``
    enriches the live roster.
    """
    from models.catalog import UnifiedCatalog
    from providers.base import ProviderModel

    model_ids: List = []

    def _build(mid, caps=None):
        return ProviderModel(id=mid, provider_id="oc", name=mid, free=True,
                             vision=False, reasoning=False,
                             context_length=8192, max_output=4096,
                             capabilities=caps or {})

    class _FakeProv:
        display_name = "oc"
        last_fetch_ok = True
        priority = 100  # base Provider's default; providers_for sorts on this

        def __init__(self, entries):
            self._entries = entries

        def list_models(self):
            out = []
            for entry in self._entries:
                if isinstance(entry, tuple):
                    mid, caps = entry
                    out.append(_build(mid, caps))
                else:
                    out.append(_build(entry))
            return out

        def is_configured(self):
            return True

    cat = UnifiedCatalog({"oc": _FakeProv(entries)})
    cat.refresh(force=True)
    return cat


def test_unit_model_map_resolve_returns_a_burned_explicit_pick():
    """resolve() rule 1: a burned real id passes through unchanged, by design.

    The Claude Code chain's rule 1 explicitly returns a real OpenCode id when
    ``catalog.providers_for(name)`` knows it -- and ``providers_for`` is the
    provider layer's bookkeeping, so it does NOT consult burn state. A burned
    explicit id therefore leaks through resolve, and the later burned-reroute
    in app.py's messages handler is what eventually intercepts it -- not a
    second burn-aware branch grafted onto resolve.

    This test pins the leak deliberately: the contract is that resolve trusts
    the provider-availability layer, not the recycler's burn pile, and the
    messages-handler burned-reroute exists *because* of rule 1.
    """
    from claudecode import model_map
    cat = _cc_recycler_catalog("alpha-free")
    # Sanity: while live, resolve returns the explicit id (rule 1).
    assert model_map.resolve("alpha-free", catalog=cat) == "alpha-free"

    while cat.record_model_failure("alpha-free") is False:
        pass
    assert cat.is_burned("alpha-free") is True
    assert "alpha-free" not in {m.id for m in cat.free()}

    # Rule 1 still fires -- providers_for knows the id and does not consult
    # burn state, so the burned id resolves to itself. That's the contract:
    # the leak is the whole reason the messages-handler burned-reroute exists.
    chosen = model_map.resolve("alpha-free", catalog=cat)
    assert chosen == "alpha-free", (
        "resolve's rule-1 leak is the contract that justifies the "
        "messages-handler burned-reroute; do not 'fix' it here"
    )


def test_unit_model_map_resolve_routes_an_alias_around_a_burned_preference():
    """resolve() rule 2: an Anthropic alias consults free(), which drops burned.

    Claude Code sends ids like ``claude-3-5-haiku``; Lingling turns them into
    the haiku size class's live-derived ranking and walks it against
    ``catalog.free()``. ``free()`` is the recycler's burn-filter funnel, so a
    burned first-preference gets skipped and the alias routes to the
    next-down one, rather than leaking through to the executor and 503ing.
    This is the alias-chain side of the recycler's "remove from everywhere"
    guarantee.

    The ranking is derived from curated capability descriptions (the same
    signals ``providers.opencode.FREE_MODEL_CAPS`` attaches to the live
    roster), never from a hardcoded id table -- so both claims pinned here
    are *behavioural*: the fast model wins haiku while live, and a burned
    leader steps aside for the next-ranked live one.
    """
    from claudecode import model_map
    fast_choice = ("alpha-fast-free", {"desc": "fast and lightweight chat; text-only"})
    deep_choice = ("beta-deep-free", {"desc": "deep multi-step reasoning for planning; text-only"})
    cat = _cc_recycler_catalog(fast_choice, deep_choice, "gamma-free")

    # Precondition -- without the burn, the haiku alias resolves to the
    # fast model, and opus to the deep one.
    assert model_map.resolve("claude-3-5-haiku", catalog=cat) == "alpha-fast-free", (
        "precondition: haiku's top live pick must be the fast model"
    )
    assert model_map.resolve("claude-opus-4-1", catalog=cat) == "beta-deep-free", (
        "precondition: opus's top live pick must be the deep-reasoning model"
    )

    while cat.record_model_failure("alpha-fast-free") is False:
        pass
    assert cat.is_burned("alpha-fast-free") is True
    assert "alpha-fast-free" not in {m.id for m in cat.free()}

    # Now the alias must skip the burned first pick -- free()'s burn-filter is
    # doing the work for resolve here, by design. Haiku's ranked pool is empty
    # (its only fast model burned), so the alias serves the next live model
    # rather than failing; opus still has its deep pick live.
    chosen = model_map.resolve("claude-3-5-haiku", catalog=cat)
    assert chosen == "beta-deep-free", (
        f"alias must skip the burned fast pick and route to the next live "
        f"model; got {chosen!r}"
    )
    assert model_map.resolve("claude-opus-4-1", catalog=cat) == "beta-deep-free", (
        "opus's deep pick must survive the haiku burn; got "
        f"{model_map.resolve('claude-opus-4-1', catalog=cat)!r}"
    )


def test_unit_a_burned_claude_code_pick_reroutes_at_the_messages_endpoint():
    """End-to-end: a burned /v1/messages explicit pick reroutes via recycler.

    Claude Code's picker sends an explicit ``model`` field (or an alias,
    which strip_alias turns into a real OpenCode id); if the recycler burned
    it because upstream rotated it out, app.py's messages handler intercepts
    the pick *before* dispatching the executor -- calling
    ``dispatcher.fallback_model(exclude={target})`` for the nearest live
    substitute. The substitute id is what surfaces in the Anthropic Messages
    response's ``lingling.routed_model`` audit key, so Claude Code's session
    trace records the model that actually answered instead of the dead id the
    client picked.

    Burns a live catalog model end-to-end to drive the cycle; the executor is
    stubbed so no real upstream call happens, and the burn heals on
    ``finally:`` so the burn state cannot leak into other tests.
    """
    # Ensure we see the real catalog, not a spy stub left by a previous hermetic test.
    _restore_catalog()
    # Heal any burn/blacklist left by previous live tests so precondition is met.
    live_ids = {m.id for m in app_mod.catalog.free()}
    if len(live_ids) < 2:
        raise SkipTest("fewer than 2 free models in the live catalog; cannot exercise reroute")
    brain = dispatcher.dispatcher_model(app_mod.catalog)
    candidates = live_ids - ({brain} if brain else set())
    if not candidates:
        raise SkipTest("only the dispatcher brain free; cannot exercise non-brain reroute")
    target = next(iter(candidates))
    # If a previous test left this target burned/blacklisted, heal it first.
    if app_mod.catalog.is_burned(target) or app_mod.catalog.is_blacklisted(target):
        app_mod.catalog.record_model_success(target)
        try:
            app_mod.catalog.clear_blacklist(target)
        except Exception:
            pass
    assert not app_mod.catalog.is_burned(target), \
        "precondition: target must start live"

    fake_ids = []
    original_exec = app_mod.executor.execute_nonstream

    def _fake_execute(messages, model_id, providers, **kwargs):
        fake_ids.append(model_id)
        canned = {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return canned, providers[0] if providers else None, None, []

    app_mod.executor.execute_nonstream = _fake_execute
    body = {
        "model": target,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        # Burn the target directly via the catalog's internal state for deterministic
        # hermetic coverage (avoids recycler threshold / blacklist races with live
        # upstream). This is the same state the messages handler checks before rerouting.
        import time as _time
        lm = app_mod.catalog._logical.get(target)
        if lm is None:
            raise SkipTest(f"target {target!r} not in catalog _logical (upstream rotated it out)")
        lm.burned = True
        lm.burned_at = _time.time()
        lm.recover_after = lm.burned_at + 3600
        assert app_mod.catalog.is_burned(target) or app_mod.catalog.is_blacklisted(target), \
            f"target {target!r} should be burned/blacklisted after direct burn"

        client = TestClient(app_mod.app)
        r = client.post("/v1/messages", json=body)
        assert r.status_code == 200, r.text
        body_json = r.json()
        lingling_meta = body_json.get("lingling") or {}
        routed_model = lingling_meta.get("routed_model")
        assert routed_model != target, (
            f"messages handler did NOT reroute: routed_model={routed_model!r} "
            f"but target={target!r} should be burned and bypassed"
        )
        assert routed_model in live_ids, (
            f"substitute {routed_model!r} is not a free model in the live catalog"
        )
        assert fake_ids, "the executor must have been called"
        assert fake_ids[0] != target, \
            "must NOT have invoked the burned id -- the reroute should bypass it"
        assert fake_ids[0] == routed_model, (
            f"executor's id {fake_ids[0]!r} should match the surfaced "
            f"routed_model {routed_model!r}"
        )
    finally:
        app_mod.executor.execute_nonstream = original_exec
        app_mod.catalog.record_model_success(target)  # un-burn for next test"

