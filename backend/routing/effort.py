"""Reasoning-effort translation between coding harnesses and OpenCode.

Every harness names thinking depth differently, and OpenCode's free models each
publish their own set of values they honour:

    Codex        none  minimal  low  medium  high  xhigh  max  ultra
    Claude Code                 low  medium  high  xhigh  max  ultracode

    deepseek-v4-flash-free      high  max          (+ an on/off toggle)
    ling-3.0-flash-free    low  medium  high
    laguna-s-2.1-free      low  medium  high
    north-mini-code-free   none            high
    mimo-v2.5-free         -- no effort control at all
    nemotron-3-ultra-free  -- no effort control at all
    big-pickle             -- no effort control at all

Those per-model sets come from models.dev's ``reasoning_options`` (see
``metadata.reasoning_effort_values``) -- the same data OpenCode's CLI shows under
``/variants``. They are read live rather than hardcoded, so a newly published
free model arrives with its real values.

Reading them is not optional. OpenCode answers **200 for an effort value a model
does not implement**, silently ignoring it, so sending ``low`` to deepseek looks
like success while changing nothing. An earlier version of this module probed the
endpoint to build its own table and was wrong for six of the seven free models
for exactly that reason.

Because the published sets are sparse and not slices of one common ladder
(``[high, max]`` vs ``[none, high]``), translation works by *rank*: the harness
label becomes a position on a 0-1 scale, and the model's nearest published value
wins. A model publishing nothing gets no parameter at all.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

# Every effort word any supported harness or provider uses, ordered weakest to
# strongest, with its position on a 0-1 scale. Rank is what crosses the boundary
# between vocabularies -- a name only has meaning relative to the others.
_RANKS: Dict[str, float] = {
    "none": 0.00,
    "minimal": 0.15,
    "low": 0.30,
    "medium": 0.50,
    "high": 0.70,
    "xhigh": 0.85,
    "max": 1.00,
    # Codex's top rung. Above `max` in its own list, so it pins to the top.
    "ultra": 1.00,
    # Claude Code's top rung: xhigh-level depth *plus* client-side workflow
    # orchestration (spawning research sub-agents). Only the depth half can cross
    # a provider boundary -- no reasoning_effort value reproduces the
    # orchestration -- so it ranks with xhigh rather than at the ceiling.
    "ultracode": 0.85,
}


def normalize(value: Any) -> Optional[str]:
    """Return the canonical effort word for a harness label, or None.

    None means the label is absent or unrecognised, which callers treat as "send
    no effort parameter" rather than as an error -- falling back to the model's
    own default beats failing a request over an unfamiliar word.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in _RANKS else None


def clamp(effort: str, allowed: Optional[Sequence[str]]) -> Optional[str]:
    """Pick the value from ``allowed`` closest in rank to ``effort``.

    ``allowed`` is the model's published ``reasoning_options`` effort list.
    Empty or None means the model exposes no effort control, and the answer is
    None -- sending a value it does not implement would be accepted and ignored,
    which is worse than not sending one because it looks like it worked.

    Matching is case- and whitespace-insensitive because ``allowed`` is
    third-party data, but the string returned is the one the provider published,
    verbatim. A published value this module has no rank for is skipped rather
    than treated as fatal, so one unfamiliar entry cannot disable effort for a
    whole model.

    Ties resolve to the weaker value, so a translation never silently spends more
    thinking than the client asked for.
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
    return min(ranked, key=lambda pair: (abs(pair[1] - want), pair[1]))[0]


def resolve(requested: Any, allowed: Optional[Sequence[str]]) -> Optional[str]:
    """Normalize then clamp in one step. None means "send no parameter"."""
    effort = normalize(requested)
    if effort is None:
        return None
    return clamp(effort, allowed)
