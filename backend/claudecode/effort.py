"""Read thinking depth out of an Anthropic Messages request.

Claude Code expresses depth three ways: ``output_config.effort`` (modern words,
already ranked in :mod:`routing.effort`); ``thinking.budget_tokens`` (older, a
token budget bucketed onto the same ladder); and ``thinking: {"type":
"disabled"}`` (an explicit no). This only *extracts* a label -- clamping to the
model's legal values happens after routing, in ``app._resolve_effort``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Budget thresholds, in thinking tokens, mapped onto the effort words
# routing.effort already ranks. Anthropic's documented floor for extended
# thinking is 1024 tokens, and Claude Code's own presets cluster around 4k
# ("think"), 10k ("think harder") and 32k ("ultrathink") -- these boundaries sit
# between those clusters rather than on them, so a preset lands unambiguously.
_BUDGET_LADDER = (
    (2_000, "low"),
    (8_000, "medium"),
    (16_000, "high"),
    (32_000, "xhigh"),
)
_BUDGET_TOP = "max"


def _from_budget(budget: int) -> str:
    """Bucket a thinking-token budget onto the shared effort ladder."""
    for ceiling, label in _BUDGET_LADDER:
        if budget <= ceiling:
            return label
    return _BUDGET_TOP


def requested_effort(body: Dict[str, Any]) -> Optional[str]:
    """The depth label a Messages request is asking for, or None to say nothing.

    ``None`` means "send no effort parameter", which lets the model use its own
    default. That is deliberately the answer for anything unrecognised: failing a
    request over an unfamiliar thinking control would be worse than answering it
    at the model's default depth.

    ``output_config.effort`` wins when both are present. Anthropic's own
    migration guide pairs it with ``thinking: {"type": "adaptive"}``, so a client
    sending both is expressing depth with the newer field and merely declaring
    that thinking is adaptive with the older one.
    """
    config = body.get("output_config")
    if isinstance(config, dict):
        effort = config.get("effort")
        if isinstance(effort, str) and effort.strip():
            # `adaptive` is a *thinking mode* that Anthropic explicitly warns is
            # not an effort value. Treated as "no opinion on depth" rather than
            # passed down to be ranked as an unknown word.
            if effort.strip().lower() != "adaptive":
                return effort

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        ttype = thinking.get("type")
        if ttype == "disabled":
            # An explicit no. Ranked as `none` so a model publishing an off rung
            # actually gets told to stop thinking, instead of being left at its
            # default because the request said nothing.
            return "none"
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget > 0:
            return _from_budget(budget)

    return None
