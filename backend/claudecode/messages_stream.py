"""OpenAI chat SSE -> Anthropic Messages SSE.

OpenAI repeats one chunk shape; Anthropic sends an explicit lifecycle a client
relies on (message_start, per-block start/delta/stop, message_delta carrying
stop_reason and usage, message_stop). So this is a state machine, and three
rules exist because breaking them fails silently:

* blocks are opened before being written to and closed before the next opens;
* indices are handed out in order of first appearance, never reserved per type;
* ``message_delta`` is emitted even when the upstream reported no finish_reason.

Tool arguments pass through as ``input_json_delta`` fragments, but the enclosing
block needs its name and id up front (OpenAI only sends them on the first).
"""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any, Dict, Generator, Optional

from claudecode import messages_response as mr


def sse(event: str, payload: Dict[str, Any]) -> bytes:
    """One Anthropic SSE frame: a named event plus its JSON body.

    Anthropic sets both the ``event:`` line and a ``type`` field inside the data,
    and real clients read either -- so both must agree.
    """
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def _parse(raw: bytes) -> Optional[Dict[str, Any]]:
    """Decode one upstream ``data:`` line, or None if it is not a chunk."""
    if not raw.startswith(b"data:"):
        return None
    payload = raw[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _delta_text(delta: Dict[str, Any]) -> str:
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _delta_reasoning(delta: Dict[str, Any]) -> str:
    """Reasoning text from a chunk, whichever field name the provider used."""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class _Blocks:
    """Tracks which content block is open, so the lifecycle stays well-formed.

    One object owns the index counter because indices are shared across text and
    tool_use blocks: a turn that says something and then calls a tool uses 0 for
    the text and 1 for the call. Nothing is reserved in advance -- a model that
    goes straight to a tool call must have that call at index 0.
    """

    def __init__(self) -> None:
        self._next = 0
        self.open_index: Optional[int] = None
        self.open_kind: Optional[str] = None

    def close(self) -> Generator[bytes, None, None]:
        """Close whatever is open. Safe to call when nothing is."""
        if self.open_index is None:
            return
        yield sse("content_block_stop", {
            "type": "content_block_stop", "index": self.open_index,
        })
        self.open_index = None
        self.open_kind = None

    def open_text(self) -> Generator[bytes, None, None]:
        """Open a text block, reusing the one already open if it is text."""
        if self.open_kind == "text":
            return
        yield from self.close()
        index = self._next
        self._next += 1
        self.open_index = index
        self.open_kind = "text"
        yield sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "text", "text": ""},
        })

    def open_thinking(self) -> Generator[bytes, None, None]:
        """Open a thinking block, reusing the one already open if it is thinking.

        Reasoning must not share a block with the answer. Merged into one text
        block the two run together -- "...No need for tools.Hi! How can I help
        you today?" -- and the model's private deliberation is rendered as its
        reply. A separate ``thinking`` block is what Anthropic's format is for,
        and clients show it collapsed.
        """
        if self.open_kind == "thinking":
            return
        yield from self.close()
        index = self._next
        self._next += 1
        self.open_index = index
        self.open_kind = "thinking"
        yield sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "thinking", "thinking": ""},
        })

    def open_tool(self, call_id: str, name: str) -> Generator[bytes, None, None]:
        """Open a tool_use block. Always a fresh one: two calls never share.

        ``input`` starts as ``{}`` and is filled by ``input_json_delta`` frames,
        which is how Anthropic streams tool arguments.
        """
        yield from self.close()
        index = self._next
        self._next += 1
        self.open_index = index
        self.open_kind = "tool"
        yield sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        })


def stream_events(
    chat_stream: Generator[bytes, None, None],
    requested_model: str,
    outcome: Optional[Any] = None,
    show_thinking: bool = False,
) -> Generator[bytes, None, None]:
    """Translate chat-completion SSE frames into Anthropic Messages SSE events.

    ``outcome`` is an optional :class:`routing.stream_guard.StreamOutcome`; when
    given, ``completed`` records whether the upstream ever reported a
    ``finish_reason``, so a stream that died mid-flight is logged as broken rather
    than filed as a success.

    ``show_thinking`` is off unless the client asked for thinking (see
    :func:`claudecode.thinking.wants_thinking`). With it off, reasoning is
    consumed and discarded -- not folded into the answer, which ran the two
    together on screen, and not emitted as its own block, which floods a terminal
    with the model's scratch work because Lingling has nothing to redact it with.
    """
    blocks = _Blocks()
    # Anthropic reports input tokens up front in message_start and output tokens
    # at the end. Neither is known here for a chat upstream, which sends usage
    # last if at all -- so message_start declares zero and message_delta carries
    # the real numbers.
    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": mr.message_id(),
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    # Tool calls arrive fragmented, keyed by an OpenAI "index" scoped to the
    # tool_calls array rather than to Anthropic's block indices. This maps one to
    # the other so a second call opens its own block.
    seen_slots: Dict[int, str] = {}
    finish_reason: Optional[str] = None
    chat_usage: Dict[str, Any] = {}
    saw_thinking = False
    saw_text = False
    stalled = False
    # Only the *size* of the reasoning is needed, to report a turn that produced no
    # answer. Retaining the text of a 5000-character think would hold it all in
    # memory for nothing.
    thinking_chars = 0

    try:
        for raw in chat_stream:
            if not raw:
                continue
            frame = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            chunk = _parse(frame)
            if chunk is None:
                continue

            if isinstance(chunk.get("usage"), dict):
                chat_usage = chunk["usage"]

            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue

            # Reasoning goes to its own thinking block, and only when the client
            # asked for it. Sharing the answer's block ran the two together on
            # screen; emitting it unrequested floods the terminal, because unlike
            # Anthropic we have no way to redact it.
            reasoning = _delta_reasoning(delta)
            if reasoning:
                saw_thinking = True
                thinking_chars += len(reasoning)
                if show_thinking:
                    yield from blocks.open_thinking()
                    yield sse("content_block_delta", {
                        "type": "content_block_delta", "index": blocks.open_index,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    })

            text = _delta_text(delta)
            if text:
                yield from blocks.open_text()
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": blocks.open_index,
                    "delta": {"type": "text_delta", "text": text},
                })
                saw_text = True

            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                slot = call.get("index")
                if not isinstance(slot, int):
                    slot = 0
                fn = call.get("function")
                fn = fn if isinstance(fn, dict) else {}
                if slot not in seen_slots:
                    # First fragment for this call: it carries the name and id.
                    name = fn.get("name")
                    call_id = call.get("id") or f"toolu_{slot}"
                    seen_slots[slot] = call_id
                    yield from blocks.open_tool(
                        call_id, name if isinstance(name, str) else "")
                arguments = fn.get("arguments")
                if isinstance(arguments, str) and arguments:
                    yield sse("content_block_delta", {
                        "type": "content_block_delta", "index": blocks.open_index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    })
    except Exception as exc:                                    # noqa: BLE001
        # An upstream that dies or goes silent mid-turn must still leave a
        # well-formed response: an unclosed block and no message_stop leaves the
        # client waiting forever, which is the hang this whole path exists to
        # avoid. Whatever arrived is kept and the turn is closed below.
        stalled = True
        if outcome is not None:
            outcome.error = str(exc)

    yield from blocks.close()

    # A model that produced only reasoning and no answer has failed the turn, and
    # the honest thing is to say so rather than pass its scratch work off as a
    # reply. Measured on a real agentic prompt: deepseek at high effort emitted
    # 1203 frames carrying 5004 characters of `reasoning_content` and an *empty*
    # `content` on every one of them. An earlier version promoted that reasoning
    # to the answer -- reasoning it was better than an empty turn -- and buried the
    # terminal in five thousand characters of deliberation.
    #
    # So: no promotion. The turn ends with a short, factual note and, where the
    # cause was the token budget, `stop_reason: max_tokens`, which is exactly what
    # a real Anthropic endpoint reports for a turn that ran out of room. An agent
    # can act on that; it cannot act on a wall of thinking.
    if saw_thinking and not saw_text and not seen_slots:
        spent = thinking_chars
        yield from blocks.open_text()
        yield sse("content_block_delta", {
            "type": "content_block_delta", "index": blocks.open_index,
            "delta": {"type": "text_delta", "text": (
                f"[{requested_model} stopped responding after reasoning for "
                f"{spent} characters. The turn was cut short; retry, or lower "
                f"the thinking depth with /effort.]"
                if stalled else
                f"[{requested_model} spent this turn reasoning "
                f"({spent} characters) without producing an answer. "
                f"Retry, lower the thinking depth with /effort, or raise max_tokens.]"
            )},
        })
        yield from blocks.close()
        # `max_tokens` is the truthful reason: the budget went entirely on
        # thinking. Left as-is when the upstream named a reason of its own.
        if finish_reason is None:
            finish_reason = "length"
    elif stalled and not saw_text and not seen_slots:
        # Nothing at all arrived before the stall, so the client would otherwise
        # get an empty turn and no explanation.
        yield from blocks.open_text()
        yield sse("content_block_delta", {
            "type": "content_block_delta", "index": blocks.open_index,
            "delta": {"type": "text_delta", "text": (
                f"[{requested_model} stopped responding before producing anything. "
                f"Retry.]"
            )},
        })
        yield from blocks.close()

    if outcome is not None:
        # A stall is not a completed turn, whatever the upstream last said.
        outcome.completed = finish_reason is not None and not stalled

    reason = mr.stop_reason(finish_reason)
    # Same correction the non-streaming path makes: a turn that emitted a tool
    # call is a tool_use turn even if the upstream forgot to say so, otherwise an
    # agent reads end_turn and stops instead of running the tool.
    if seen_slots and reason == "end_turn":
        reason = "tool_use"

    counts = mr.usage(chat_usage)
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": reason, "stop_sequence": None},
        "usage": {"input_tokens": counts["input_tokens"],
                  "output_tokens": counts["output_tokens"]},
    })
    yield sse("message_stop", {"type": "message_stop"})
