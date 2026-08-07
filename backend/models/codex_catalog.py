"""Generate the model catalog Codex reads, so effort reaches the model.

Codex decides whether to put ``reasoning: {"effort": ...}`` on the wire by
looking up the model in its own catalog. A model it does not know gets the field
nulled -- verified against a capture proxy: with Codex's stock catalog a request
for ``deepseek-v4-flash-free`` carries ``"reasoning": null`` no matter what
``model_reasoning_effort`` says. Declare the same model in a
``model_catalog_json`` file with a non-empty ``supported_reasoning_levels`` and
the identical request carries ``{"effort": "high", "summary": "auto"}``.

This module builds that file's contents. Two facts shape it:

*Every entry is a clone of a real Codex model.* The schema has 35 fields, one of
which is ``base_instructions`` -- the entire Codex agent prompt (11-21 KB
depending on the model). A hand-written entry with a short prompt parses fine and
then quietly turns Codex into a much dumber agent, because that string *is* the
harness. So the caller passes a template dumped from the installed binary
(``codex debug models``) and only identity, reasoning levels and limits are
overridden. Cloning also keeps the file valid across Codex upgrades that add
fields.

*Overrides must never write null.* Codex's parser dumps ``null`` for absent
optional fields but rejects it on input for some of them -- setting
``web_search_tool_type`` to null fails with "expected value", reproducibly. Any
value this module writes is therefore a real value, and fields it has no opinion
about are left exactly as the template had them.

Effort levels come from ``LogicalModel.capabilities["effort"]``, i.e. models.dev's
per-model ``reasoning_options``, so what Codex offers is what the model honours.
Lingling still clamps on the way out (``app._resolve_effort``): Codex sends
whatever its config says even when the value is not in the declared list.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# Picker copy for each effort word, in the voice of the descriptions Codex ships.
# Required by the parser, and shown next to the level in Codex's model picker; the
# wire only ever carries the effort word itself, so this text has no effect on how
# hard a model thinks. `none` and `minimal` have no counterpart in the stock
# catalog and are worded to match.
_LEVEL_DESCRIPTIONS: Dict[str, str] = {
    "default": "The model's own default depth (it exposes no dial)",
    "none": "No reasoning; answer directly",
    "minimal": "Barely any reasoning, for the simplest tasks",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Maximum reasoning with automatic task delegation",
}

# Stand-in level for a model that publishes no effort options at all. Codex only
# lists a model whose level list is non-empty, so without this mimo, nemotron and
# big-pickle were absent from `/model` entirely. `default` is deliberately a word
# ``routing.effort`` has no rank for: it normalises to None, so Lingling forwards
# *no* effort parameter and the model runs on its own default -- which is the only
# thing these models can do. The moment models.dev publishes real options for one
# of them, the published set wins and this disappears on the next run.
_DEFAULT_LEVEL = "default"

# Fields Codex's parser demands. A caller passing a hand-made template gets a
# clear failure here instead of an "expected value at line 1 column N" from the
# Rust side after the file is written.
REQUIRED_TEMPLATE_FIELDS = (
    "slug",
    "display_name",
    "supported_reasoning_levels",
    "shell_type",
    "visibility",
    "supported_in_api",
    "priority",
    "base_instructions",
    # Codex 0.146 renamed the old boolean `supports_reasoning_summaries` to this
    # string field (values like "none"/"auto"). It is inherited verbatim from the
    # template; requiring it keeps the validation aligned with the installed
    # binary's own dump instead of the schema this module was first written
    # against.
    "default_reasoning_summary",
    "support_verbosity",
    "truncation_policy",
    "supports_parallel_tool_calls",
    "experimental_supported_tools",
)


def levels_for(effort_values: Iterable[str]) -> List[Dict[str, str]]:
    """Turn models.dev effort values into Codex ``supported_reasoning_levels``.

    Order follows the provider's published list. Values with no known picker
    description are skipped rather than given invented copy -- an unrecognised
    word is more likely a schema change than a level worth advertising.

    ``effort_values`` is third-party data, so a non-iterable (models.dev once
    publishing a bare number, say) yields an empty list rather than raising.
    """
    if isinstance(effort_values, (str, bytes)) or not isinstance(effort_values, Iterable):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for value in effort_values:
        if not isinstance(value, str):
            continue
        word = value.strip().lower()
        if word in seen or word not in _LEVEL_DESCRIPTIONS:
            continue
        seen.add(word)
        out.append({"effort": word, "description": _LEVEL_DESCRIPTIONS[word]})
    return out


def entry_for(
    template: Dict[str, Any],
    model_id: str,
    display_name: str,
    effort_values: Iterable[str],
    context_length: Optional[int] = None,
    vision: bool = False,
    priority: int = 1,
    description: str = "",
) -> Dict[str, Any]:
    """Clone ``template`` into a catalog entry for one Lingling model."""
    missing = [f for f in REQUIRED_TEMPLATE_FIELDS if f not in template]
    if missing:
        raise ValueError(f"template is not a Codex catalog model, missing: {', '.join(missing)}")
    if not model_id:
        raise ValueError("model_id is required; an empty slug is silently useless to Codex")

    levels = levels_for(effort_values)
    if not levels:
        # No published options: offer the single `default` rung so Codex still
        # lists the model. Sending that word resolves to "no effort parameter",
        # which is exactly what such a model needs.
        levels = levels_for([_DEFAULT_LEVEL])
    entry = dict(template)
    entry.update({
        "slug": model_id,
        "display_name": display_name,
        "supported_reasoning_levels": levels,
        # The picker's initial highlight, not the wire default: Codex sends the
        # effort from its own config (falling back to `minimal`) regardless of
        # what this says. Weakest declared level, so nothing here spends more
        # thinking than the user asked for.
        "default_reasoning_level": levels[0]["effort"],
        # Summary display follows the template's `default_reasoning_summary`
        # (inherited verbatim from the clone). Codex 0.146 replaced the old
        # boolean `supports_reasoning_summaries` with this string field, and
        # injecting the stale key back would risk the Rust parser rejecting the
        # file -- same reason nothing else here writes a field the template does
        # not carry.
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "input_modalities": ["text", "image"] if vision else ["text"],
        "supports_image_detail_original": bool(vision),
        # Cleared, not inherited: these advertise ChatGPT-plan features (a "Fast"
        # speed tier, a new-model announcement popup naming the template model)
        # that mean nothing for a locally-proxied free model. `null` is accepted
        # here even though it is rejected for some other fields.
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
    })

    if description:
        entry["description"] = description
    if context_length:
        entry["context_window"] = context_length
        entry["max_context_window"] = context_length
    return entry


def build(
    template: Dict[str, Any],
    models: Iterable[Dict[str, Any]],
    auto_id: Optional[str] = None,
    auto_name: str = "Lingling Auto",
    auto_description: str = "",
) -> Dict[str, Any]:
    """Build the whole ``model_catalog_json`` payload.

    ``models`` are Lingling catalog models (``LogicalModel.to_dict()`` shape).
    Only those publishing effort values are listed: a model with no effort control
    gains nothing from being declared, and leaving it out keeps Codex's own
    handling of an unknown model, which works today.

    ``auto_id`` adds an entry for the ``lingling-auto`` router. Its levels are the
    union of every listed model's, because the routed model -- and therefore the
    legal set -- is not known until the dispatcher has run. Lingling clamps the
    value against whatever it picks.
    """
    entries: List[Dict[str, Any]] = []
    union: List[str] = []
    contexts: List[int] = []
    seen_ids: set = set()
    for model in models:
        if model.get("context_length"):
            contexts.append(int(model["context_length"]))
        model_id = model.get("id") or ""
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        # Every free model is listed. Those publishing real effort options get
        # them; the rest get the single `default` rung, which keeps them visible
        # in Codex's picker and forwards no effort parameter.
        effort_values = (model.get("capabilities") or {}).get("effort") or []
        entries.append(entry_for(
            template,
            model_id=model_id,
            display_name=model.get("name") or model_id,
            effort_values=effort_values,
            context_length=model.get("context_length"),
            vision=bool(model.get("vision")),
            priority=len(entries) + 2,      # after lingling-auto
            description=model.get("description") or "",
        ))
        for word in levels_for(effort_values):
            if word["effort"] not in union:
                union.append(word["effort"])


    if auto_id and entries:
        # Ranked ladder, so `default_reasoning_level` lands on the weakest rung
        # rather than on whichever model happened to be listed first.
        order = list(_LEVEL_DESCRIPTIONS)
        union.sort(key=order.index)
        entries.insert(0, entry_for(
            template,
            model_id=auto_id,
            display_name=auto_name,
            effort_values=union,
            # The smallest window any routable model has. Codex compacts against
            # this number, and the router may land on any of them, so the floor is
            # the only value that cannot overflow the model it actually picks.
            context_length=min(contexts) if contexts else None,
            vision=True,               # the router can reach a vision model
            priority=1,
            description=auto_description,
        ))
    return {"models": entries}



