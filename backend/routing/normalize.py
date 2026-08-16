"""Normalise assistant messages so reasoning upstreams accept them.

OpenCode's reasoning models (big-pickle, deepseek, hy3, ...) run in "thinking
mode" and require that assistant turns carry a string ``reasoning_content``
when they are replayed in a later request -- and that the field is a string
whenever it is present at all. Clients do not always cooperate: some drop the
field entirely, some echo it back as a non-string (an object or list). Both
shapes get the request rejected with a 400 from the upstream, and since every
free model is reasoning-capable, all three models fail and the router answers
503.

We cannot recover text the client never sent, but the upstream accepts an
empty string, so the rule is:

* a tool-calling or empty assistant turn is guaranteed a string
  ``reasoning_content`` -- ``""`` when the client dropped it (verified
  accepted on all three free models, with and without a reasoning effort);
* a ``reasoning_content`` that is not a string is salvaged when it looks like
  text parts and otherwise removed, so a malformed object can never break JSON
  deserialisation on the upstream;
* a plain text turn is left alone -- it must not gain a spurious empty key.

Everything else is left untouched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _salvage_reasoning(value: Any) -> Optional[str]:
    """Text from a non-string ``reasoning_content``, or None when unusable."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str) and part:
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "thinking", "content"):
                    text = part.get(key)
                    if isinstance(text, str) and text:
                        parts.append(text)
                        break
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("text", "thinking", "content", "reasoning"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
    return None


def _assistant_has_text(message: Dict[str, Any]) -> bool:
    """True when the assistant turn carries real text content."""
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(part, dict) and isinstance(part.get("text"), str)
            and part["text"].strip()
            for part in content
        )
    return False


def normalize_reasoning_content(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make assistant turns presentable to reasoning upstreams, in place."""
    if not isinstance(messages, list):
        return messages
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        rc = message.get("reasoning_content")
        if rc is not None and not isinstance(rc, str):
            # Non-string: salvage text, otherwise drop the malformed field so
            # the upstream never tries to deserialise an object into a string.
            good = _salvage_reasoning(rc)
            if good:
                message["reasoning_content"] = good
                continue
            message.pop("reasoning_content", None)
        if not isinstance(message.get("reasoning_content"), str):
            # Tool-calling or empty turns lose the key in clients and get a 400
            # on replay; the upstream accepts an empty string, so guarantee it
            # there. A plain text turn must not gain a spurious empty key.
            if message.get("tool_calls") or not _assistant_has_text(message):
                message["reasoning_content"] = ""
    return messages