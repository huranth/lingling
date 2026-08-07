"""OpenAI chat-completion response -> Anthropic Messages response.

Three mappings carry the weight:

* **``finish_reason`` -> ``stop_reason``.** Anthropic's vocabulary is different
  and a client branches on it, so ``length`` must become ``max_tokens`` and
  ``tool_calls`` must become ``tool_use`` -- a wrong value here makes an agent
  mis-handle a perfectly good turn.
* **Flat text -> a content block list.** Anthropic answers are always blocks,
  and a ``tool_calls`` response becomes one ``tool_use`` block per call.
* **Usage field names.** ``prompt_tokens``/``completion_tokens`` become
  ``input_tokens``/``output_tokens``.

Reasoning is returned only when the client asked for thinking, and then in its
own ``thinking`` block -- never folded into the answer. Both of the other two
arrangements were tried against a real session and both were worse: concatenated,
the deliberation reads as the reply; emitted unrequested, it floods the terminal,
because Anthropic redacts thinking by default and Lingling has nothing to redact
with. See :func:`claudecode.thinking.wants_thinking`.

A turn that produces no visible answer at all is reported as such rather than
having its scratch work promoted -- see :func:`_assistant_text`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

# finish_reason -> stop_reason. Anything unrecognised falls back to end_turn:
# a client that branches on stop_reason copes with a normal ending, where an
# unknown value could raise.
_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def stop_reason(finish_reason: Any) -> str:
    """Translate a chat ``finish_reason`` into an Anthropic ``stop_reason``."""
    if not isinstance(finish_reason, str):
        return "end_turn"
    return _STOP_REASONS.get(finish_reason, "end_turn")


def message_id() -> str:
    return f"msg_{int(time.time() * 1000):x}"


def usage(chat_usage: Any) -> Dict[str, int]:
    """Map chat usage onto Anthropic's field names.

    ``cache_creation_input_tokens``/``cache_read_input_tokens`` are reported as 0
    rather than omitted: Claude Code reads them when it accounts for a session,
    and a missing key is a KeyError where an honest zero is just "no caching".
    """
    if not isinstance(chat_usage, dict):
        chat_usage = {}
    return {
        "input_tokens": int(chat_usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(chat_usage.get("completion_tokens", 0) or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _reasoning_text(message: Dict[str, Any]) -> str:
    """The model's reasoning, whichever field name the provider used.

    Returned separately from the answer so it can go in a ``thinking`` block. The
    two must not be concatenated: a client renders the result as the reply, and the
    user reads the model's deliberation as its response.
    """
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _assistant_text(message: Dict[str, Any]) -> str:
    """The visible text of an assistant message. Reasoning is *not* a fallback.

    An earlier version fell back to the reasoning fields when ``content`` was
    empty, so a model that spent its whole turn thinking had its scratch work
    presented as the answer. Measured on a real agentic prompt, that was 5000
    characters of deliberation rendered as a reply. The caller handles the
    no-answer case explicitly instead.
    """
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [str(p.get("text", "")) for p in content if isinstance(p, dict)]
        joined = "\n".join(p for p in parts if p)
        if joined.strip():
            return joined
    return ""


def _tool_use_block(call: Any) -> Optional[Dict[str, Any]]:
    """One chat tool_call -> one Anthropic ``tool_use`` block.

    Chat completions carries arguments as a JSON *string*; Anthropic's ``input``
    is a parsed object. A model that emits malformed JSON would otherwise take the
    whole response down, so an unparseable payload becomes an empty input and the
    call still reaches the client with its name intact.
    """
    if not isinstance(call, dict):
        return None
    fn = call.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw = fn.get("arguments")
    parsed: Any = {}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
    elif isinstance(raw, dict):
        parsed = raw
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "type": "tool_use",
        "id": call.get("id") or f"toolu_{int(time.time() * 1000):x}",
        "name": name,
        "input": parsed,
    }


def response_object(
    chat: Dict[str, Any], requested_model: str, routed_model: str, provider: str,
    show_thinking: bool = False,
) -> Dict[str, Any]:
    """Build the Anthropic Messages response for a chat completion.

    ``requested_model`` is echoed back rather than the model that actually ran,
    because a client compares the response model against what it asked for. Where
    the two differ, the routed model is reported under a ``lingling`` key, which
    is additive and ignored by clients that do not know it.

    ``show_thinking`` gates the reasoning entirely -- see
    :func:`claudecode.thinking.wants_thinking` for why the default is off.
    """
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    content: List[Dict[str, Any]] = []
    reasoning = _reasoning_text(message)
    visible = _assistant_text(message)

    if show_thinking and reasoning and visible and reasoning != visible:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if visible:
        content.append({"type": "text", "text": visible})
    for call in message.get("tool_calls") or []:
        block = _tool_use_block(call)
        if block is not None:
            content.append(block)

    reason = stop_reason(choice.get("finish_reason"))
    # A turn carrying tool calls is a tool_use turn even when the upstream said
    # `stop` -- some free models finish a tool call without setting the reason,
    # and an agent reading end_turn there stops instead of running the tool.
    if reason == "end_turn" and any(b.get("type") == "tool_use" for b in content):
        reason = "tool_use"

    # A turn with no visible answer at all: the model reasoned and stopped. Say so
    # plainly rather than dressing its scratch work up as a reply -- see
    # `_assistant_text` for what that looked like in practice.
    if not content:
        content.append({"type": "text", "text": (
            f"[{requested_model} spent this turn reasoning ({len(reasoning)} "
            f"characters) without producing an answer. Retry, lower the thinking "
            f"depth with /effort, or raise max_tokens.]"
            if reasoning else
            f"[{requested_model} returned an empty turn.]"
        )})
        if reason == "end_turn" and reasoning:
            # The budget went entirely on thinking, which is what max_tokens means.
            reason = "max_tokens"

    return {
        "id": chat.get("id") or message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": reason,
        "stop_sequence": None,
        "usage": usage(chat.get("usage")),
        "lingling": {"routed_model": routed_model, "provider": provider},
    }

