"""Reasoning-effort translation between coding harnesses and OpenCode.

Harnesses and models each name thinking depth differently and publish
different value sets, so translation works by *rank*: a harness label becomes a
position on a 0-1 scale and the model's nearest published value wins. A model
publishing nothing gets no parameter.

Gotcha: OpenCode returns 200 for an effort value a model does not implement,
silently ignoring it -- so a value must never be sent unless the model's
published list contains it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

# Effort words ordered weakest to strongest, each mapped to a 0-1 rank. Rank is
# what crosses the boundary between harness/model vocabularies.
_RANKS: Dict[str, float] = {
    "none": 0.00,
    "minimal": 0.15,
    "low": 0.30,
    "medium": 0.50,
    "high": 0.70,
    "xhigh": 0.85,
    "max": 1.00,
    "ultra": 1.00,       # Codex's top rung, pins to the ceiling
    "ultracode": 0.85,   # Claude Code's top rung; only its depth half can cross
}


def normalize(value: Any) -> Optional[str]:
    """Canonical effort word for a harness label, or None if absent/unknown.

    None means "send no effort parameter" -- falling back to the model's own
    default beats failing a request over an unfamiliar word.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in _RANKS else None


def clamp(effort: str, allowed: Optional[Sequence[str]]) -> Optional[str]:
    """Pick the value from ``allowed`` closest in rank to ``effort``.

    ``allowed`` is the model's published effort list; empty/None returns None
    (the model exposes no effort control). Unknown published values are skipped
    rather than treated as fatal. Ties resolve to the weaker value.
    """
    if not allowed:
        return None
    ranked = []
    for value in allowed:
        if not isinstance(value, str):
            continue
        rank = _RANKS.get(value.strip().lower())
        if rank is not None:
            ranked.append((value, rank))
    if not ranked:
        return None
    want = _RANKS.get(effort)
    if want is None:
        return None
    # Round the distance before comparing: raw float drift (0.7-0.5 ==
    # 0.19999999999999996) made an exact midpoint resolve to the stronger value,
    # contradicting the weaker-wins tie rule.
    return min(ranked, key=lambda pair: (round(abs(pair[1] - want), 10), pair[1]))[0]


def resolve(requested: Any, allowed: Optional[Sequence[str]]) -> Optional[str]:
    """Normalize then clamp. None means "send no parameter"."""
    effort = normalize(requested)
    if effort is None:
        return None
    return clamp(effort, allowed)

