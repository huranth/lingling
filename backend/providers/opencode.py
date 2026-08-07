"""OpenCode provider.

OpenCode Zen (https://opencode.ai/zen/v1) is an OpenAI-compatible gateway whose
free tier is keyless: ``GET /models`` and the free models on
``POST /chat/completions`` both answer with no credential. The web-created
"OpenCode Zen API key" only gates paid models, which Lingling does not serve,
so Lingling talks to OpenCode keyless by default. Configured account keys
(``accounts.json``) are optional for the free tier.

A model is free by OpenCode's own ``-free`` suffix, or by being a known keyless
free model listed in ``FREE_MODEL_CAPS`` (e.g. ``big-pickle``). models.dev
pricing is intentionally not consulted -- it mislabels some paid OpenCode
models as free -- so anything else is treated as premium (fail closed).

OpenCode's ``/models`` returns only ``{id, object, created, owned_by}`` (no
modalities), and models.dev has gaps. ``FREE_MODEL_CAPS`` is a curated overlay
of hand-verified notes, not a gate: a ``-free`` model without an entry is still
served with capabilities inferred from models.dev and the id itself, so new
free models appear with no code change.
routing dispatcher reads when a hand-checked description exists.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core import config
from models import metadata
from providers.base import OpenAICompatibleProvider


def _infer_desc(model_id: str, live: Dict[str, Any]) -> str:
    """A human-readable strengths line for a model the curated overlay lacks.

    Built from live models.dev metadata plus the id itself, so a newly
    published free model is fully first-class (dashboard and routing
    dispatcher) with no code change. Reads like the curated ``desc`` lines so
    the dispatcher's strength markers behave consistently.
    """
    parts: List[str] = []
    if live.get("vision"):
        parts.append("multimodal: understands images and screenshots")
    if live.get("reasoning"):
        parts.append("strong reasoning")
    ctx = live.get("context_length")
    if ctx:
        parts.append(f"{int(ctx) // 1000}K context")
    mid = model_id.lower()
    if any(tok in mid for tok in ("code", "codex", "dev", "engin")):
        parts.append("good at coding and engineering")
    if any(tok in mid for tok in ("flash", "mini", "nano", "lite")):
        parts.append("fast and lightweight")
    if not parts:
        parts.append("general-purpose assistant")
    joined = "; ".join(parts)
    return joined + ("; text-only" if not live.get("vision") else "")


def _infer_caps(model_id: str, live: Dict[str, Any]) -> Dict[str, Any]:
    """Conservative capability flags for a model with no curated entry."""
    mid = model_id.lower()
    caps: Dict[str, Any] = {
        "vision": bool(live.get("vision")),
        "reasoning": bool(live.get("reasoning")),
    }
    if any(tok in mid for tok in ("code", "codex", "dev", "engin")):
        caps["coding"] = True
    return caps


def direct_model_ids(catalog) -> frozenset:
    """Ids that bypass the egress pool for latency -- computed live.

    Always the routing dispatcher (a stalled dispatcher blocks every
    ``lingling-auto`` turn), plus the fastest free text-only chat model, taken
    as the smallest published context window -- the only latency signal
    models.dev publishes. A newly published fast model joins automatically;
    nothing here is a hardcoded id list.
    """
    ids = set([config.DISPATCHER_MODEL])
    best: Optional[str] = None
    best_ctx = 0
    for m in catalog.free():
        if m.vision:
            continue
        ctx = m.context_length or 0
        if best is None or ctx < best_ctx:
            best, best_ctx = m.id, ctx
    if best:
        ids.add(best)
    return frozenset(ids)


_catalog_ref = None
_DIRECT_CACHE: frozenset = frozenset()
_DIRECT_AT = 0.0
_DIRECT_TTL_S = 300.0


def set_catalog_ref(catalog) -> None:
    """Give the provider the live catalog so ``prefer_direct`` stays dynamic."""
    global _catalog_ref
    _catalog_ref = catalog


def _current_direct_ids() -> frozenset:
    global _DIRECT_CACHE, _DIRECT_AT
    if _catalog_ref is None:
        return frozenset({config.DISPATCHER_MODEL})
    now = time.time()
    if not _DIRECT_CACHE or now - _DIRECT_AT > _DIRECT_TTL_S:
        _DIRECT_CACHE = direct_model_ids(_catalog_ref)
        _DIRECT_AT = now
    return _DIRECT_CACHE


# Authoritative capabilities of OpenCode's free-tier models. Verified live
# (vision/tool-call probes against the real endpoint) and cross-checked against
# each vendor's docs. Any free model not listed here gets conservative defaults.
# `desc` is the human-readable strength summary the dispatcher reads.
#
# NOTE: OpenCode's free tier is identified by the ``-free`` suffix OR by being a
# known keyless free model (``big-pickle``). models.dev pricing is NOT trusted
# for OpenCode because it mislabels some paid models (e.g. ``gpt-5.3-codex-spark``)
# as free -- only entries here are served.
#
# Reasoning effort is deliberately absent from this map. models.dev publishes a
# per-model ``reasoning_options`` list -- the same data OpenCode's CLI shows under
# ``/variants`` -- and it is not uniform: deepseek honours low/high/max, ling
# honours low/medium/high, mimo honours none. It cannot be probed either, because
# OpenCode returns 200 for a value the model ignores. So effort comes from
# metadata.reasoning_effort_values() and stays current as models change; see
# routing/effort.py.
FREE_MODEL_CAPS: Dict[str, Dict[str, Any]] = {
    "deepseek-v4-flash-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "huge context, deep thinking/reasoning mode, excellent at coding and long documents; text-only (no images)",
    },
    "mimo-v2.5-free": {
        "vision": True, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "multimodal: understands images/screenshots/photos; also strong general reasoning; the ONLY free vision model",
    },
    "ling-3.0-flash-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "balanced general-purpose chat and light coding; fast; text-only",
    },
    "nemotron-3-ultra-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "NVIDIA model tuned for deep multi-step reasoning, math and planning; text-only",
    },
    "north-mini-code-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "specialized for code generation, refactoring and software engineering; text-only",
    },
    "laguna-s-2.1-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "solid general-purpose assistant for everyday questions and writing; text-only",
    },
    # Big Pickle is OpenCode's own free reasoning model (keyless, confirmed live).
    # No -free suffix, so it must be listed here to be served.
    "big-pickle": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "strong deliberate reasoning model for multi-step problem solving and tool use; text-only",
    },
    # Meituan's LongCat-2.0, served by OpenCode as longcat-2.0-free. Carries the
    # -free suffix so it is already served; this curated entry gives the
    # dispatcher its real strengths instead of guessing from the id. 1M-token
    # context, 131K output, tool calling, reasoning, text-only (confirmed live +
    # modeled in models.dev: attachment=false, modalities=[text], limit.context=1M).
    "longcat-2.0-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "Meituan 1M-context reasoning model with tool calling; excellent for very long documents and in-depth coding; text-only",
    },
}


class OpenCodeProvider(OpenAICompatibleProvider):
    id = "opencode"
    display_name = "OpenCode"
    base_url = config.OPENCODE_BASE_URL
    priority = 10                 # preferred provider
    all_free = False

    def requires_key(self) -> bool:
        # OpenCode Zen's free tier is keyless (verified live): /models AND the
        # free models on /chat/completions answer with no credential. A Zen
        # API key only gates paid models, which Lingling doesn't serve.
        return False

    def needs_proxy(self) -> bool:
        # OpenCode rate-limits its free tier by the connecting IP (Redis daily +
        # lifetime counters, plus a MySQL token ledger, all keyed on IP). The
        # only effective countermeasure is rotating the egress IP, so OpenCode
        # requests are routed through the proxy pool when one is configured.
        return True

    def prefer_direct(self, model_id: str) -> bool:
        """Keep the fast chat route responsive when local WARP is unhealthy.

        Fast-path models bypass the egress proxy pool entirely. The set is
        computed live from the catalog (the routing dispatcher plus the fastest
        free text-only chat model) rather than hardcoded, so a newly published
        fast model joins automatically. ``LINGLING_FAST_MODELS_DIRECT=0``
        forces everything through the pool.
        """
        if not config.FAST_MODELS_DIRECT:
            return False
        if model_id == config.DISPATCHER_MODEL:
            return True   # a stalled dispatcher blocks every lingling-auto turn
        return model_id in _current_direct_ids()

    def _models_secret(self) -> Optional[str]:
        return None               # /models is keyless on OpenCode

    def is_model_free(self, model_id: str, meta: Dict[str, Any]) -> bool:
        # OpenCode's explicit free marker.
        if model_id.lower().endswith("-free"):
            return True
        # Known keyless free models without the suffix (e.g. big-pickle). Only
        # curated entries count -- models.dev pricing is intentionally NOT
        # trusted here because it mislabels paid models as free.
        if model_id in FREE_MODEL_CAPS:
            return True
        # Everything else is treated as premium (fail closed).
        return False

    def build_model(self, model_id: str):
        """Enrich a model id, combining curated notes with live models.dev data.

        OpenCode's own ``/models`` returns nothing but ``{id, object, created,
        owned_by}``, so capabilities have to come from elsewhere. The split:

        * ``FREE_MODEL_CAPS`` owns vision, tool-calling and the ``desc`` the
          dispatcher reads -- verified by hand, because models.dev has gaps and
          mislabels some paid models as free.
        * models.dev owns context limits and the reasoning-effort values, because
          both change per model release and cannot be probed reliably. An earlier
          hardcoded copy of these drifted badly: it claimed 1M context for
          deepseek (really 200K) and seven effort levels for models that honour
          two or none.
        """
        from providers.base import ProviderModel, _prettify
        live = metadata.enrich(model_id, self.id)

        # The curated overlay is optional enrichment, not a gate. It upgrades a
        # hand-verified description when present; a model it does not cover is
        # still served and described dynamically (from models.dev + the id), so
        # a newly published free model appears and routes on the next catalog
        # refresh, with no code change.
        caps: Dict[str, Any] = dict(FREE_MODEL_CAPS.get(model_id) or {})
        caps.setdefault("effort", live.get("effort") or [])
        caps.setdefault("reasoning_toggle", bool(live.get("reasoning_toggle")))
        caps.setdefault("desc", _infer_desc(model_id, live))
        for key, value in _infer_caps(model_id, live).items():
            caps.setdefault(key, value)

        return ProviderModel(
            id=model_id,
            provider_id=self.id,
            name=live.get("name") or _prettify(model_id),
            free=self.is_model_free(model_id, live.get("_meta") or {}),
            vision=bool(caps.get("vision")),
            reasoning=bool(caps.get("reasoning")),
            context_length=live.get("context_length"),
            max_output=live.get("max_output"),
            modalities=live.get("modalities") or (
                ["text", "image"] if caps.get("vision") else ["text"]
            ),
            capabilities=caps,
        )
