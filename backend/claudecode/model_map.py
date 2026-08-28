"""Which OpenCode model a Claude Code request actually means.

Claude Code sends Anthropic model ids -- ``claude-sonnet-4-6``, ``claude-opus-5``
and so on -- and also makes background calls on whatever id resolves for its
``haiku`` alias (session titles, conversation summaries). None of those exist
here, so a request naming one has to be resolved to a real free model or it 404s.

Two rules, in order:

1. **An OpenCode model id passes through untouched.** Setting
   ``ANTHROPIC_MODEL=deepseek-v4-flash-free`` is the honest way to pin a model,
   and it must keep working.
2. **Anything else routes by size class.** ``haiku`` -> fast, ``sonnet`` -> the
   general-purpose default, ``opus`` -> the deepest thinker available. The
   per-class ranking is derived from the *live* catalog at resolve time -- the
   same capability signals the routing dispatcher reads -- so the targets are
   whatever the official /models endpoint is currently advertising, never a
   hardcoded roster that upstream rotations silently stale. An unrecognised id
   gets the dispatcher, which is the same answer Lingling gives any client that
   asks for something it cannot name.
"""

from __future__ import annotations

from typing import Any, List, Optional

from core import config

# Anthropic size classes, in the order their names appear in a model id. Order
# matters: `claude-3-5-haiku` contains neither "sonnet" nor "opus", but a future
# id could name two tiers, and the first match should win.
_CLASSES = ("haiku", "sonnet", "opus")

# Capability tokens used to rank a live model for a size class, best first.
# These are the same signals the routing dispatcher reads (curated provider
# desc first, then id-token inference), so a new fast or deep model upstream
# puts in is adopted by the aliases without a code change.
_FAST_TOKEN = ("flash", "mini", "nano", "lite")
_DEEP_TOKEN = ("ultra", "pro", "max", "large", "big", "deep")


def _desc(model: Any) -> str:
    caps = getattr(model, "capabilities", None)
    if isinstance(caps, dict) and caps.get("desc"):
        return str(caps["desc"]).lower()
    return ""


def _class_score(model: Any, cls: str) -> int:
    """How well ``model`` fits an Anthropic size class, from live signals.

    A curated provider desc (``providers.opencode.FREE_MODEL_CAPS``) is the
    strongest signal when present; otherwise the score leans on id tokens.
    """
    desc = _desc(model)
    mid = model.id.lower()
    if cls == "haiku":
        # Fast casual chat. A desc that says so is the strongest signal; a fast
        # token in the id counts for less (``deepseek-v4-flash-free`` carries
        # "-flash" in its speed rating yet is a deep, slow-thinking model).
        score = 2 if any(w in desc for w in ("fast", "lightweight", "quick")) else 0
        return score + (1 if any(t in mid for t in _FAST_TOKEN) else 0)
    if cls == "opus":
        # The deepest thinker available. Deep reasoning wins; the id's
        # "big"/"ultra"-style token breaks ties.
        score = 2 if any(
            w in desc for w in ("deep", "multi-step", "deliberate", "planning", "math")
        ) else 0
        return score + (1 if any(t in mid for t in _DEEP_TOKEN) else 0)
    # sonnet: the general-purpose default -- an all-rounder for everyday work,
    # with coding and a big context window as second axes.
    score = 2 if any(
        w in desc for w in ("general-purpose", "general purpose", "balanced", "everyday")
    ) else 0
    if "cod" in desc:
        score += 1
    if (model.context_length or 0) >= 100_000:
        score += 1
    return score


def _preferred(cls: str, catalog: Any) -> List[str]:
    """Live free model ids that show a signal for ``cls``, ranked best first.

    ``catalog.free()`` is the recycler's burn filter, so a burned or rotated-out
    preference never appears here; equal scores keep ``free()``'s deterministic
    order. Empty when no live model shows a signal for the class -- the caller
    then serves the alias from the best live model regardless.
    """
    pool = list(catalog.free())
    ranked = sorted(pool, key=lambda m: (-_class_score(m, cls), pool.index(m)))
    return [m.id for m in ranked if _class_score(m, cls) > 0]


def size_class(model_id: str) -> Optional[str]:
    """The Anthropic size class named in a model id, or None."""
    lowered = model_id.lower()
    for name in _CLASSES:
        if name in lowered:
            return name
    return None


def resolve(model_id: Any, catalog: Any) -> str:
    """Return the OpenCode model id a Claude Code request should route to.

    ``catalog`` is the live :class:`models.catalog.UnifiedCatalog`. It is consulted
    rather than trusted blindly: an alias target that is not currently served
    (upstream dropped it, or it is premium today) is skipped, so this cannot hand
    routing a model that would immediately 503.
    """
    if not isinstance(model_id, str) or not model_id:
        return config.MULTIMODEL_ID

    # Discovery advertises free models under a `claude-` prefix, because Claude
    # Code drops any id that lacks one. Undo that first so a model picked from the
    # list is recognised here instead of looking like an unknown Anthropic id.
    candidates = [model_id]
    unprefixed = strip_alias(model_id)
    if unprefixed != model_id:
        candidates.append(unprefixed)

    for name in candidates:
        # Rule 1: a real model id, or Lingling's own auto id, is already an answer.
        if name == config.MULTIMODEL_ID or catalog.providers_for(name):
            return name
        bare = name.split("/", 1)[1] if "/" in name else name
        if bare != name and catalog.providers_for(bare):
            return bare

    # Rule 2: route by what the Anthropic name implies about depth. The ranking
    # is derived from the live catalog, so burned or rotated-out preferences are
    # gone (``free()`` is the burn filter) and a newly-advertised fast or deep
    # model is adopted without a code change. A class with no signal in any
    # live model (a minimal catalog, or models without curated descriptions)
    # still resolves to the best live model rather than failing the request.
    cls = size_class(model_id)
    if cls:
        ranked = _preferred(cls, catalog)
        if ranked:
            return ranked[0]
        pool = list(catalog.free())
        if pool:
            return pool[0].id

    # Unrecognised, or no live model at all: let the dispatcher decide, the
    # same as any other client.
    return config.MULTIMODEL_ID


def advertised_models(catalog: Any) -> List[dict]:
    """The ``GET /v1/models`` list Claude Code's model discovery will accept.

    Claude Code ignores any entry whose id does not begin with ``claude`` or
    ``anthropic``, so every free model is also advertised under a ``claude-``
    prefixed alias.

    Not wired into ``/v1/models``: that endpoint is shared with Codex and Cline,
    and adding aliases there would show every free model twice in their pickers.
    Claude Code's discovery is opt-in anyway
    (``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1``), and setting
    ``ANTHROPIC_MODEL`` explicitly is the documented path. Kept because
    :func:`resolve` accepts the alias form either way.
    """
    out: List[dict] = []
    for model in catalog.free():
        out.append({
            "type": "model",
            "id": f"claude-{model.id}",
            "display_name": f"{model.name} (Lingling)",
        })
    out.append({
        "type": "model",
        "id": f"claude-{config.MULTIMODEL_ID}",
        "display_name": f"{config.MULTIMODEL_NAME} (Lingling)",
    })
    return out


def strip_alias(model_id: Any) -> Any:
    """Undo the ``claude-`` prefix :func:`advertised_models` adds.

    Applied before resolution so a model picked from the discovery list resolves
    to the real free model rather than being treated as an unknown Anthropic id
    and handed to the dispatcher.
    """
    if isinstance(model_id, str) and model_id.startswith("claude-"):
        return model_id[len("claude-"):]
    return model_id
