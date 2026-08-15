"""Whether a Claude Code request wants to see the model's thinking."""

from __future__ import annotations

from typing import Any, Dict


def wants_thinking(body: Dict[str, Any]) -> bool:
    """True only when the client explicitly enabled thinking.

    Default is off, matching Anthropic. Lingling cannot redact reasoning (an
    OpenCode model's thinking is unsigned plain text), so an unrequested
    thinking block would render in full and bury the answer.
    """
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
        return True
    return False

