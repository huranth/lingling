"""Whether a Claude Code request wants to see the model's thinking.

Its own module because the answer gates three separate paths -- the non-stream
response builder, the stream translator, and the endpoint -- and getting it wrong
is spectacular rather than subtle: an unrequested thinking block floods the
terminal with the model's entire scratch work.
"""

from __future__ import annotations

from typing import Any, Dict


def wants_thinking(body: Dict[str, Any]) -> bool:
    """True when the client explicitly asked to see the model's thinking.

    The default is **no**, which is what Anthropic itself does. Its settings
    reference is explicit: with ``alwaysThinkingEnabled`` unset, "thinking blocks
    are redacted by the API and shown as a collapsed stub". Claude Code does not
    hide thinking on its own; it relies on the API having redacted it first.

    Lingling cannot redact anything -- an OpenCode model's reasoning is plain text
    with no signature and no encryption. So an unrequested ``thinking`` block gets
    rendered in full, and a model reasoning at length buries the answer. Dropping
    it is both the honest reading of the request and the only behaviour that
    matches what a real Anthropic endpoint returns.
    """
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
        return True
    return False
