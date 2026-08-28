"""Unified multi-provider catalog of free models.

Each provider lists its own models; this catalog merges them into one view.
Models are grouped by id into a :class:`LogicalModel` that records *which
providers serve it*. That grouping is what enables cross-provider failover:
``deepseek-v4-flash-free`` served by OpenCode becomes one
logical model; the executor can then try different egress proxies in turn
when one IP burns.

Only free models become logical models; premium models are counted as filtered.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config
from models.catalog_persistence import BurnStateStore
from providers.base import Provider, ProviderModel

# Same ``uvicorn.error`` root the daemon ``self._log``-gers and the health/tor
# managers share, so the recycler's burn / heal / cooldown-emits surface at
# INFO threshold alongside the rest of the daemon's state-change logs and the
# operator can grep one namespace for every "is the gateway misbehaving?" line.
_log = logging.getLogger("uvicorn.error")


class LogicalModel:
    """A free model id and the providers that serve it."""

    def __init__(self, primary: ProviderModel) -> None:
        self.id = primary.id
        self.primary = primary
        self.providers: Dict[str, ProviderModel] = {primary.provider_id: primary}
        self.consecutive_failures: int = 0
        self.last_failure_at: float = 0.0
        self.burned: bool = False
        self.burned_at: float = 0.0
        self.recover_after: float = 0.0  # auto-retry timestamp; 0 = no trial scheduled
        # Reach from disk via ``BurnStateStore``. ``blacklisted`` is the
        # operator-quality "stop probing" flag; ``blacklist_hits`` counts
        # back-to-back reconcile failures and trips ``blacklisted`` once it
        # crosses ``config.BURN_BLACKLIST_HITS``. ``burned`` is unchanged:
        # recycled by the live recycler (``record_model_failure`` /
        # ``record_model_success``); the blacklist is the strict superset.
        self.blacklisted: bool = False
        self.blacklist_hits: int = 0

    @property
    def retired(self) -> bool:
        """Whether this model id is on the operator's ``LINGLING_RETIRED_MODELS`` list.

        Driven by the seed config rather than probe verdict: the model is hidden
        on every refresh so the dispatcher can never pick it, even before the
        recycler has had a chance to observe a failure. The flag is re-derived
        from ``config`` on every ``refresh`` / ``apply_persisted_state`` call so
        editing the env var is felt on the next refresh without touching
        on-disk state.
        """
        return self.id in config.LINGLING_RETIRED_MODELS

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


def _apply_retired_seed(logical: Dict[str, LogicalModel]) -> int:
    """Reconcile the operator's ``LINGLING_RETIRED_MODELS`` seed against the catalog.

    Two-way reconciliation so editing the env var is felt on the next refresh
    without touching the on-disk manifest:
    * Each id in the seed is forced onto ``blacklisted=True`` so dispatch
      cannot pick it, even before the recycler has had a chance to observe a
      failure.
    * Each id that is currently blacklisted with ``blacklist_hits == 0`` (so
      the only reason it is blacklisted is the seed itself, not an operator or
      the probe path) AND is NOT in the seed any more is cleared, so removing a
      model from the env var promptly returns it to the rotation.

    Reusing the blacklist flag (instead of a parallel filter) means the retire
    seed rides the same persist/rehydrate pipeline as operator blacklists, so
    the ``free()`` filter, the dashboard and the dispatcher all see it through
    one funnel. Returns the number of *changes* made (set + cleared) so callers
    can log the diff.
    """
    retired = set(config.LINGLING_RETIRED_MODELS or ())
    changes = 0
    for mid, lm in logical.items():
        if mid in retired:
            if not lm.blacklisted:
                lm.blacklisted = True
                changes += 1
        elif lm.blacklisted and lm.blacklist_hits == 0:
            lm.blacklisted = False
            changes += 1
    return changes


class UnifiedCatalog:
    """Merges every provider's free models into one routable catalog."""

    def __init__(self, providers: Dict[str, Provider], persist_path: Optional[Path] = None) -> None:
        self.providers = providers
        # Reentrant lock -- mutating helpers (``mark_blacklisted``, ``clear_blacklist``,
        # ``record_model_failure`` etc.) can be called from inside a read that is
        # already holding ``_lock`` (the reconciler's ``_classify`` does exactly
        # this). A plain ``threading.Lock`` would deadlock in that path; ``RLock``
        # matches the "any helper can be called whether lock is held or not"
        # contract the ``_*_locked`` private functions already imply.
        self._lock = threading.RLock()
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
        # Persistent burn-state store. Lives outside the in-memory dict so the
        # recycler's verdict on a model still holds after a clean restart; the
        # reconciler reads/applies the on-disk manifest at startup and the
        # catalog rewrites it after every burn / heal / blacklist transition.
        self._burn_store = BurnStateStore(
            Path(persist_path) if persist_path is not None else config.BURN_STATE_FILE
        )

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
                        lm = LogicalModel(pm)
                        prior = self._logical.get(pm.id)  # carry burn state across refresh
                        if prior is not None:
                            lm.consecutive_failures = prior.consecutive_failures
                            lm.last_failure_at = prior.last_failure_at
                            lm.burned = prior.burned
                            lm.burned_at = prior.burned_at
                            lm.recover_after = prior.recover_after
                            lm.blacklisted = prior.blacklisted
                            lm.blacklist_hits = prior.blacklist_hits
                        logical[pm.id] = lm
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
            # Re-stamp the operator retire list on every refresh so it can't be
            # aged out by TTL/cooldown and survives an empty manifest on disk.
            seeded = _apply_retired_seed(self._logical)
            if seeded:
                _log.info(
                    "catalog: applied %d retired-model seed entr%s from %s",
                    seeded, "y" if seeded == 1 else "ies", "LINGLING_RETIRED_MODELS",
                )
            # NO persist call here -- on first boot, the manifest on disk
            # carries burns from the previous process; if we wrote our
            # still-empty in-memory state at this point, we'd clobber it
            # before apply_persisted_state could read it. ``record_model_*``
            # and ``apply_persisted_state`` own persists from here on.
            return self

    # -- queries -----------------------------------------------------------
    def free(self) -> List["LogicalModel"]:
        """Every free model not currently burned, sorted vision-first then by id.

        Burned models are dropped here -- this is the single funnel every
        candidate pool, client listing and picker derives from, so this filter
        stamps them out of dispatcher candidate sets, ``fallback_model``,
        ``model_map.resolve``'s Claude Code chain, the codex dump,
        ``/api/models`` and ``/v1/models`` end-to-end, without those callers
        needing their own burn logic. Falls back to root-[``self._is_burned_locked``]
        so the cooldown-auto-recover tick also fires here, on the read path.
        Blacklisted models (reconcile proved Zen will not serve them) are
        dropped too, by the same funnel, so phone-home reauth / manual
        clear-list are the only paths back.
        """
        self.refresh()
        with self._lock:
            return [
                lm for lm in sorted(self._logical.values(),
                                    key=lambda m: (not m.vision, m.id))
                if not self._is_burned_locked(lm) and not lm.blacklisted
            ]

    def vision_free(self) -> List["LogicalModel"]:
        return [lm for lm in self.free() if lm.vision]

    # -- recycler: burn / heal / cooldown ----------------------------------
    def record_model_failure(self, model_id: str) -> bool:
        """Bump a model's consecutive-failure counter; burn it past the threshold.

        Returns True iff this call newly transitioned the model to ``burned``
        (i.e. the counter just crossed ``config.MAX_MODEL_FAILURES``). Returns
        False otherwise (already burned, or model unknown to the catalog).

        A burned model is dropped from ``free()`` / ``vision_free()`` and from
        every client-facing listing; after ``config.MODEL_BURN_COOLDOWN_SECONDS``
        it gets one trial request, at which point a fresh success un-burns it.
        """
        with self._lock:
            lm = self._logical.get(model_id)
            if lm is None:
                return False
            lm.consecutive_failures += 1
            lm.last_failure_at = time.time()
            if not lm.burned and lm.consecutive_failures >= config.MAX_MODEL_FAILURES:
                lm.burned = True
                lm.burned_at = lm.last_failure_at
                lm.recover_after = lm.burned_at + config.MODEL_BURN_COOLDOWN_SECONDS
                _log.info(
                    "recycler: model %s just tanked for the %dth time up the chain -- "
                    "dropping it from the rotation; back in the kitchen in %ds if it behaves",
                    lm.id, lm.consecutive_failures, int(config.MODEL_BURN_COOLDOWN_SECONDS),
                )
                self._persist_state()
                return True
            return False

    def record_model_success(self, model_id: str) -> None:
        """Reset a model's failure streak; heal a burned model that just succeeded."""
        transitioned = False
        with self._lock:
            lm = self._logical.get(model_id)
            if lm is None:
                return
            lm.consecutive_failures = 0
            lm.last_failure_at = 0.0
            if lm.burned:
                lm.burned = False
                lm.burned_at = 0.0
                lm.recover_after = 0.0
                transitioned = True
                _log.info(
                    "recycler: model %s cooked clean on the trial -- back in the kitchen",
                    lm.id,
                )
            # A clean return-trip also resets reconcile-trail hits; the
            # ``blacklisted`` flag is operator-controlled and survives.
            if lm.blacklist_hits:
                lm.blacklist_hits = 0
                transitioned = True
        if transitioned:
            self._persist_state()

    def is_burned(self, model_id: str) -> bool:
        """Whether ``model_id`` is currently burned; consumes any cooldown elapsed."""
        with self._lock:
            lm = self._logical.get(model_id)
            if lm is None:
                return False
            return self._is_burned_locked(lm)

    def _maybe_auto_recover(self, lm: "LogicalModel") -> None:
        # Caller must hold ``self._lock``.
        # A blacklisted model must NOT auto-recover on cooldown -- the blacklist
        # is the operator/reconciler saying "Zen will not serve this; stop
        # wasting a free-tier slot on it". Only an explicit un-blacklist call
        # brings it back.
        if lm.blacklisted:
            return
        if lm.burned and lm.recover_after and time.time() >= lm.recover_after:
            lm.burned = False
            lm.recover_after = 0.0
            _log.info(
                "recycler: model %s's cooldown lapsed -- letting it back into the kitchen for one shift",
                lm.id,
            )

    def _is_burned_locked(self, lm: "LogicalModel") -> bool:
        # Caller must hold ``self._lock``. Burns-pop-on-cooldown lives here so
        # ``free()`` -- the single funnel every candidate path & listing derives
        # from -- drives the auto-recover tick instead of needing its own.
        self._maybe_auto_recover(lm)
        return lm.burned

    # -- blacklist / reconcile surface area -------------------------------
    def is_blacklisted(self, model_id: str) -> bool:
        """Whether ``model_id`` is on the operator/reconcile blacklist."""
        with self._lock:
            lm = self._logical.get(model_id)
            return bool(lm and lm.blacklisted)

    def mark_blacklisted(
        self, model_id: str, hits: int, probe_status: Optional[int],
    ) -> bool:
        """Reconcile-trail update: bump blacklist_hits; trip blacklisted on threshold.

        Called by the reconciler after a probe confirms the model is still
        broken. ``hits`` is the new consecutive-probe-broken count; the
        threshold lives on the reconciler, the catalog only persists what it
        is told. ``probe_status`` is captured for the operator log. Returns
        True iff this call newly flipped the model to blacklisted.
        """
        with self._lock:
            lm = self._logical.get(model_id)
            if lm is None:
                return False
            lm.blacklist_hits = max(0, int(hits))
            newly_blacklisted = False
            if config.BURN_BLACKLIST_HITS > 0 and not lm.blacklisted and \
                    lm.blacklist_hits >= config.BURN_BLACKLIST_HITS:
                lm.blacklisted = True
                newly_blacklisted = True
                _log.warning(
                    "recycler: model %s probed broken %d times in a row "
                    "(status=%s) -- hard-blacklisted until an operator clears it",
                    lm.id, lm.blacklist_hits, probe_status,
                )
        self._persist_state()
        return newly_blacklisted

    def clear_blacklist(self, model_id: str) -> bool:
        """Operator un-blacklist. Returns True iff the model was blacklisted."""
        with self._lock:
            lm = self._logical.get(model_id)
            if lm is None:
                return False
            if not lm.blacklisted and not lm.blacklist_hits:
                return False
            was_blacklisted = lm.blacklisted
            lm.blacklisted = False
            lm.blacklist_hits = 0
        self._persist_state()
        return was_blacklisted

    def apply_persisted_state(self) -> int:
        """Read the on-disk burn manifest and re-hydrate the catalog.

        Called once at startup, after the first ``refresh()`` has rebuilt
        ``_logical``. Any persisted entry whose model id no longer exists on
        any provider is left untouched on disk -- a future refresh on a
        healthier upstream will rediscover the model and pick the manifest
        back up. Returns the number of entries applied (for logging).
        """
        manifest = self._burn_store.load()
        if not manifest:
            return 0
        applied = 0
        with self._lock:
            for mid, entry in manifest.items():
                lm = self._logical.get(mid)
                if lm is None:
                    continue
                lm.burned = bool(entry.get("burned"))
                lm.burned_at = float(entry.get("burned_at", 0.0))
                lm.recover_after = float(entry.get("recover_after", 0.0))
                lm.consecutive_failures = int(entry.get("consecutive_failures", 0))
                lm.blacklisted = bool(entry.get("blacklisted"))
                lm.blacklist_hits = int(entry.get("blacklist_hits", 0))
                applied += 1
            # Re-stamp the operator retire list AFTER the disk rehydration so a
            # currently-empty list that the operator added mid-process is felt
            # on the next reload, and any model the disk says is fine but the
            # env var wants hidden is forced onto the blacklist here.
            _apply_retired_seed(self._logical)
        if applied:
            _log.info(
                "burn-state: rehydrated %d manifest entr%s from %s",
                applied, "y" if applied == 1 else "ies", self._burn_store.path,
            )
            # Persist the merged (in-memory + on-disk) state so the file
            # carries the canonical bled-through view, not whatever shape
            # it had on the previous boot.
            self._persist_state()
        return applied

    def _persist_state(self) -> None:
        """Snapshot every LogicalModel's burn trail to disk.

        Takes ``self._lock`` itself (the lock is an ``RLock``, so callers that
        already hold it -- ``record_model_failure``, ``mark_blacklisted``,
        ``apply_persisted_state`` -- nest safely) so the snapshot cannot catch
        a half-applied transition (e.g. ``burned=True`` with a stale
        ``burned_at``) from the reconciler thread, which calls this without the
        lock. ``BurnStateStore`` then takes its own internal lock for the
        actual filesystem write.
        """
        with self._lock:
            snapshot: Dict[str, Dict[str, Any]] = {}
            for mid, lm in self._logical.items():
                snapshot[mid] = {
                    "burned": lm.burned,
                    "blacklisted": lm.blacklisted,
                    "blacklist_hits": lm.blacklist_hits,
                    "consecutive_failures": lm.consecutive_failures,
                    "burned_at": lm.burned_at,
                    "recover_after": lm.recover_after,
                }
        self._burn_store.save(snapshot)

    def by_id(self, model_id: str) -> Optional[LogicalModel]:
        self.refresh()
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
        if model_id in self._logical:
            return True
        for pm in self._all_models:
            if pm.id == model_id:
                return False
        return None

    def meta(self) -> Dict[str, Any]:
        self.refresh()
        logical = self._logical
        return {
            "generated_at": self._generated_at,
            "free": len(logical),
            "vision_free": len([lm for lm in logical.values() if lm.vision]),
            "total_models": len(self._all_models),
            "premium_filtered": len([m for m in self._all_models if not m.free]),
            "multi_provider_models": len([lm for lm in logical.values() if len(lm.providers) > 1]),
            "burned": len([lm for lm in logical.values() if lm.burned]),
            "blacklisted": len([lm for lm in logical.values() if lm.blacklisted]),
            "free_only": True,
            "providers": self._per_provider,
            "burn_state_file": str(self._burn_store.path),
            # True when *every* provider's list is a stale cached fallback, i.e.
            # the last refresh could not reach any upstream. The dashboard can
            # warn instead of implying the (cached) list is freshly confirmed.
            "stale": bool(self._stale) and all(self._stale.values()),
        }
