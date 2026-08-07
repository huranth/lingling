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
   general-purpose default, ``opus`` -> the deepest thinker available. An
   unrecognised id gets the dispatcher, which is the same answer Lingling gives
   any client that asks for something it cannot name.

The alias targets are configurable but deliberately *not* hardcoded to a specific
model list: catalogs change, and a missing target falls back to whatever the
catalog does offer rather than failing the request.
"""

from __future__ import annotations

from typing import Any, List, Optional

from core import config

# Anthropic size classes, in the order their names appear in a model id. Order
# matters: `claude-3-5-haiku` contains neither "sonnet" nor "opus", but a future
# id could name two tiers, and the first match should win.
_CLASSES = ("haiku", "sonnet", "opus")


def size_class(model_id: str) -> Optional[str]:
    """The Anthropic size class named in a model id, or None."""
    lowered = model_id.lower()
    for name in _CLASSES:
        if name in lowered:
            return name
    return None


def _pick_for_class(catalog: Any, cls: str) -> Optional[str]:
    """Choose the free model that best matches an Anthropic size class.

    Selected live from the catalog: haiku wants the fastest and cheapest
    (smallest context) text-only model, opus the deepest thinker, sonnet the
    general-purpose default. Ranking reads the same curated descriptions the
    routing dispatcher uses, so a newly published model competes with no code
    change -- and it never hands routing a model the catalog is not currently
    serving.
    """
    pool = [m for m in catalog.free() if not m.vision]
    if not pool:
        pool = list(catalog.free())
    if not pool:
        return None

    def _score(m):
        caps = getattr(m, "capabilities", None) or {}
        desc = str(caps.get("desc") or "").lower()
        ctx = m.context_length or 0
        if cls == "haiku":
            fast = 2 if any(w in desc for w in ("fast", "lightweight", "flash")) else 0
            return (fast, -ctx)
        if cls == "opus":
            deep = any(w in desc for w in
                       ("deep", "multi-step", "deliberate", "planning", "math", "reason"))
            return ((2 if m.reasoning else 0) + (2 if deep else 0), ctx)
        # sonnet: the general, well-rounded default.
        general = any(w in desc for w in
                      ("general-purpose", "general purpose", "balanced", "everyday"))
        return ((2 if general else 0), ctx)

    return max(pool, key=_score).id


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

    # Rule 2: route by what the Anthropic name implies about depth. Picked live
    # from the catalog, so a newly published model joins the mapping
    # automatically -- no hardcoded id list.
    cls = size_class(model_id)
    if cls:
        picked = _pick_for_class(catalog, cls)
        if picked:
            return picked

    # Unrecognised: let the dispatcher decide, the same as any other client.
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
