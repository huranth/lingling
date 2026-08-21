"""Unified multi-provider catalog of free models.

Each provider lists its own models; this catalog merges them by id into a
:class:`LogicalModel` that records which providers serve it. That grouping is
what enables cross-provider failover. Only free models become logical models;
premium ones are counted as filtered.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config
from providers.base import Provider, ProviderModel


class LogicalModel:
    """A free model id and the providers that serve it."""

    def __init__(self, primary: ProviderModel) -> None:
        self.id = primary.id
        self.primary = primary
        self.providers: Dict[str, ProviderModel] = {primary.provider_id: primary}

    def add(self, pm: ProviderModel) -> None:
        self.providers[pm.provider_id] = pm

    @property
    def provider_ids(self) -> List[str]:
        return list(self.providers.keys())

    @property
    def name(self) -> str:
        return self.primary.name

    @property
    def free(self) -> bool:
        return True

    # Capabilities are intrinsic to the model; combine across provider variants
    # robustly (vision/reasoning if any variant reports it, max context/output).
    @property
    def vision(self) -> bool:
        return any(p.vision for p in self.providers.values())

    @property
    def reasoning(self) -> bool:
        return any(p.reasoning for p in self.providers.values())

    @property
    def context_length(self) -> Optional[int]:
        vals = [p.context_length for p in self.providers.values() if p.context_length]
        return max(vals) if vals else None

    @property
    def max_output(self) -> Optional[int]:
        vals = [p.max_output for p in self.providers.values() if p.max_output]
        return max(vals) if vals else None

    @property
    def modalities(self) -> List[str]:
        mods: set = set()
        for p in self.providers.values():
            mods.update(p.modalities)
        return sorted(mods)

    @property
    def capabilities(self) -> Dict[str, Any]:
        # Merge curated capability notes from the underlying provider variants;
        # the first non-empty `desc` wins so the dispatcher reads a concrete
        # strength summary rather than guessing from the model id.
        for p in self.providers.values():
            caps = getattr(p, "capabilities", None)
            if isinstance(caps, dict) and caps.get("desc"):
                return caps
        return {}

    def to_dict(self) -> Dict[str, Any]:
        caps = self.capabilities
        return {
            "id": self.id,
            "name": self.name,
            "free": True,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "context_length": self.context_length,
            "max_output": self.max_output,
            "modalities": self.modalities,
            "providers": self.provider_ids,
            "provider_count": len(self.providers),
            # Curated capability notes (coding / tool_calls / desc). The dashboard
            # renders these; the dispatcher already reads them via `.capabilities`.
            "capabilities": caps,
            "description": caps.get("desc", "") if isinstance(caps, dict) else "",
        }


class UnifiedCatalog:
    """Merges every provider's free models into one routable catalog."""

    def __init__(self, providers: Dict[str, Provider]) -> None:
        self.providers = providers
        self._lock = threading.Lock()
        self._logical: Dict[str, LogicalModel] = {}
        self._all_models: List[ProviderModel] = []
        self._per_provider: Dict[str, Dict[str, Any]] = {}
        self._generated_at: float = 0.0
        # Last successfully-fetched model list per provider. A provider whose
        # /models call fails on a refresh keeps its previous list instead of
        # vanishing from the catalog, so a transient upstream blip never leaves
        # the dashboard/CLI model picker empty.
        self._last_good: Dict[str, List[ProviderModel]] = {}
        self._stale: Dict[str, bool] = {}
        # When the last refresh *attempt* ran, successful or not. Needed
        # separately from `_generated_at`: an empty catalog fails the TTL guard
        # below, so without this every request would re-run a failing upstream
        # fetch and concurrent callers would queue behind each other.
        self._attempted_at: float = 0.0
        # Advertised-`-free` models that refused to serve (OpenCode answers
        # "This model is unavailable for free"). Learned at runtime when a
        # request hits that 400, so the catalog stops offering models the
        # upstream has retired even though it still advertises them.
        self._unavailable: Dict[str, float] = {}
        # Operator-curated dead ids (config.retired_seed_ids). Honored verbatim
        # and excluded from the runtime self-heal in refresh(): the seed exists
        # precisely because /models keeps advertising a model the operator knows
        # is dead, so a live re-appearance is NOT authoritative for a seeded id.
        self._seed_ids: set = set(config.retired_seed_ids())
        self._load_unavailable()

    # -- retired models ----------------------------------------------------
    def _load_unavailable(self) -> None:
        """Restore the retired set: seeded known-dead ids plus persisted runtime ones.

        Seeded ids (``config.retired_seed_ids()``) are hidden from the very
        first startup -- OpenCode keeps advertising dropped models, so the
        runtime 400-learning only triggers on first use. They are re-stamped
        ``now`` on every load, so they never expire while they remain seeded.
        Runtime-learned entries (below) keep their TTL.
        """
        ttl = config.RETIRED_MODEL_TTL_DAYS * 86400
        now = time.time()
        self._unavailable = {}
        for mid in config.retired_seed_ids():
            self._unavailable[mid] = now
        try:
            raw = json.loads(Path(config.RETIRED_MODELS_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            for mid, ts in raw.items():
                if isinstance(ts, (int, float)) and (now - float(ts)) < ttl:
                    self._unavailable[str(mid)] = float(ts)

    def _save_unavailable(self) -> None:
        try:
            path = Path(config.RETIRED_MODELS_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._unavailable), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def mark_unavailable(self, model_id: str) -> None:
        """Hide a model from the catalog because the upstream retired its free tier.

        Called when a request to an advertised-`-free` model comes back with the
        "unavailable for free" 400. The model is dropped from every query and
        the fact is persisted so the Codex catalog generator (a separate
        process) agrees. The entry expires after ``RETIRED_MODEL_TTL_DAYS``, so
        a restored free tier reappears without a manual step.
        """
        with self._lock:
            self._unavailable[model_id] = time.time()
            self._logical.pop(model_id, None)
            self._all_models = [m for m in self._all_models if m.id != model_id]
            self._save_unavailable()

    def is_unavailable(self, model_id: str) -> bool:
        return model_id in self._unavailable

    def _active(self, items) -> List[LogicalModel]:
        return [m for m in items if m.id not in self._unavailable]

    def retired(self) -> List[str]:
        """Retired model ids, for the dashboard to show rather than guess."""
        return sorted(self._unavailable)

    def refresh(self, force: bool = False) -> "UnifiedCatalog":
        with self._lock:
            now = time.time()
            if not force:
                if self._logical and (now - self._generated_at) < config.CATALOG_TTL_SECONDS:
                    return self
                # Empty catalog: back off on the *attempt* clock so a failing or
                # genuinely-empty upstream is retried periodically instead of on
                # every single request.
                if not self._logical and (now - self._attempted_at) < config.CATALOG_RETRY_SECONDS:
                    return self
            self._attempted_at = now
            logical: Dict[str, LogicalModel] = {}
            all_models: List[ProviderModel] = []
            per_provider: Dict[str, Dict[str, Any]] = {}
            for pid, prov in self.providers.items():
                models = prov.list_models()
                # If the live fetch failed, fall back to the last good list for
                # this provider so a momentary /models outage does not empty the
                # catalog. A provider that genuinely returns zero models (fetch
                # succeeded, list empty) correctly replaces its cache.
                stale = False
                if not models and not getattr(prov, "last_fetch_ok", True):
                    models = self._last_good.get(pid, [])
                    stale = bool(models)
                else:
                    self._last_good[pid] = models
                self._stale[pid] = stale
                free_count = 0
                for pm in models:
                    all_models.append(pm)
                    if not pm.free:
                        continue
                    free_count += 1
                    lm = logical.get(pm.id)
                    if lm is None:
                        logical[pm.id] = LogicalModel(pm)
                    else:
                        lm.add(pm)
                per_provider[pid] = {
                    "display_name": prov.display_name,
                    "total": len(models),
                    "free": free_count,
                    "configured": prov.is_configured(),
                    # True when this list is the cached fallback because the
                    # live fetch just failed -- surfaced in meta() for the UI.
                    "stale": stale,
                }
            self._logical = logical
            self._all_models = all_models
            self._per_provider = per_provider
            self._generated_at = time.time()

            # Self-heal runtime retirements against the live free section. A
            # -free model retired at runtime was absent from /models at retirement
            # time; if a later refresh sees it back in the free section, OpenCode
            # has not actually removed it -- the outage was transient upstream
            # overload, exactly the case the auto-recycle must NOT lock out for
            # the full TTL (7 days) just because it 400'd once. Reconcile only
            # against authoritative (non-stale) provider fetches, so a fallback
            # to the cached last-good list cannot resurrect an id the upstream
            # genuinely stopped offering. Operator-seeded ids are left alone: the
            # seed exists precisely because /models keeps advertising a model the
            # operator knows is dead, so the live list is not authoritative for
            # it.
            reconciled = False
            for mid in list(self._unavailable):
                if mid in self._seed_ids:
                    continue
                lm = logical.get(mid)
                if lm is None:
                    continue
                if any(not self._stale.get(pid, False) for pid in lm.provider_ids):
                    self._unavailable.pop(mid, None)
                    reconciled = True
            if reconciled:
                self._save_unavailable()
            return self

    # -- queries -----------------------------------------------------------
    def free(self) -> List[LogicalModel]:
        self.refresh()
        active = self._active(self._logical.values())
        return sorted(active, key=lambda lm: (not lm.vision, lm.id))

    def vision_free(self) -> List[LogicalModel]:
        return [lm for lm in self.free() if lm.vision]

    def by_id(self, model_id: str) -> Optional[LogicalModel]:
        """Resolve a model from the current catalog view.

        Runs on every routed request, so it does not re-fetch when the model is
        already known -- an explicit request must not pay an upstream round-trip
        just because the TTL happened to expire. Only an unknown model falls
        through to a refresh, so a just-published model still resolves by name.
        """
        if model_id in self._unavailable:
            return None
        lm = self._logical.get(model_id)
        if lm is not None:
            return lm
        self.refresh()
        if model_id in self._unavailable:
            return None
        return self._logical.get(model_id)

    # alias for callers that prefer .get
    get = by_id

    def providers_for(self, model_id: str) -> List[Provider]:
        """Providers serving this free model, configured-first then by priority."""
        lm = self.by_id(model_id)
        if not lm:
            return []
        provs = [self.providers[pid] for pid in lm.provider_ids if pid in self.providers]
        provs.sort(key=lambda p: (not p.is_configured(), p.priority))
        return provs

    def is_free(self, model_id: str) -> Optional[bool]:
        """True if free, False if known-but-premium, None if unknown."""
        self.refresh()
        if model_id in self._unavailable:
            return None
        if model_id in self._logical:
            return True
        for pm in self._all_models:
            if pm.id == model_id:
                return False
        return None

    def meta(self) -> Dict[str, Any]:
        self.refresh()
        logical = self._logical
        active = self._active(list(logical.values()))
        return {
            "generated_at": self._generated_at,
            "free": len(active),
            "vision_free": len([lm for lm in active if lm.vision]),
            "total_models": len(self._all_models),
            "premium_filtered": len([m for m in self._all_models if not m.free]),
            "multi_provider_models": len([lm for lm in active if len(lm.providers) > 1]),
            # Advertised-`-free` models the upstream refused to serve, learned at
            # runtime. Surfaced so the dashboard can show them rather than have
            # them silently vanish (or worse, keep being offered).
            "retired": len(self._unavailable),
            "retired_models": self.retired(),
            "free_only": True,
            "providers": self._per_provider,
            "stale": bool(self._stale) and all(self._stale.values()),
        }
