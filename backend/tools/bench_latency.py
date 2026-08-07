"""Benchmark: request-path latency — before vs after the latency optimizations.

Isolates the two things we changed for an *explicitly-named* model (not
lingling-auto), excluding the model's own thinking time:

  RESOLVE (model id -> provider/egress target)
    OLD: `by_id()` called `refresh()` every request, so one request in every
         10-minute TTL window paid a synchronous upstream `/models` round-trip
         (~200 ms spike). The other requests in the window were cached.
    NEW: `by_id()` serves the cached view always -> no spike ever.

  CONNECT (get an HTTP client to the upstream through the egress)
    OLD: warm tunnels were pruned after 30 s idle, so a turn after a short gap
         paid a fresh SOCKS5 + TLS handshake (~120 ms).
    NEW: warm tunnels are kept up to 180 s, so a turn after the same gap reuses
         the handshake (~0 ms).

The upstream round-trip and the handshake are simulated with the constants
below so the comparison is deterministic and shows the mechanism.

Run:  cd backend && py -3 tools/bench_latency.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from models.catalog import UnifiedCatalog  # noqa: E402
from providers.base import Provider, ProviderModel  # noqa: E402
from providers.key_pool import KeyPool  # noqa: E402
from providers.connection_pool import ConnectionPool  # noqa: E402

SIMULATED_MODELS_ROUNDTRIP_S = 0.20   # one OpenCode /models (+ models.dev) round-trip
SIMULATED_HANDSHAKE_S = 0.12          # one SOCKS5 + TLS handshake


class FastProvider(Provider):
    id = "opencode"
    display_name = "OpenCode"
    priority = 10

    def __init__(self, ids):
        super().__init__(KeyPool([]))
        self._ids = ids

    def requires_key(self):
        return False

    def fetch_model_ids(self):
        time.sleep(SIMULATED_MODELS_ROUNDTRIP_S)
        return self._ids

    def is_model_free(self, mid, meta):
        return mid.endswith("-free")

    def build_model(self, mid):
        return ProviderModel(
            id=mid, provider_id=self.id, name=mid, free=True,
            vision=False, reasoning=True, context_length=200000, max_output=100,
        )


class FakeHttpx:
    is_closed = False

    def close(self):
        self.is_closed = True


def legacy_by_id(cat, model_id):
    """Pre-fix by_id: refresh() first, so an expired cache re-fetches."""
    cat.refresh()
    if cat.is_unavailable(model_id):
        return None
    return cat._logical.get(model_id)  # noqa: SLF001


def fake_create_client(proxy_url, timeout):
    time.sleep(SIMULATED_HANDSHAKE_S)
    return FakeHttpx()


def bench_resolve_spike():
    """Worst-case request inside a refresh window (the 200 ms spike)."""
    cat = UnifiedCatalog({"opencode": FastProvider(["alpha-free"])})
    cat._generated_at = 0.0  # cache just expired -> OLD must re-fetch

    t0 = time.perf_counter()
    legacy_by_id(cat, "alpha-free")
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    cat.by_id("alpha-free")
    new_ms = (time.perf_counter() - t0) * 1000
    return old_ms, new_ms


def bench_connect_gap():
    """A turn after an idle gap: cold (pruned) vs warm (kept)."""
    pool = ConnectionPool(
        max_clients_per_proxy=2,
        client_idle_timeout_s=float(config.CONNECTION_POOL_IDLE_S),
    )
    pool._create_client = fake_create_client  # noqa: SLF001

    with pool.get_client("proxy-1", "socks5://127.0.0.1:51001", 30.0):
        pass

    # OLD: the pre-fix 30s keepalive means a turn after a >30s idle has no warm
    # tunnel -> a fresh handshake. Make the client appear long-idle and prune.
    pool._clients["proxy-1"][0].last_used = time.time() - 60.0  # noqa: SLF001
    pool.prune_idle(max_idle_s=31.0)
    t0 = time.perf_counter()
    with pool.get_client("proxy-1", "socks5://127.0.0.1:51001", 30.0):
        pass
    old_ms = (time.perf_counter() - t0) * 1000

    # NEW: same gap is inside the 180 s window -> warm client reused.
    t0 = time.perf_counter()
    with pool.get_client("proxy-1", "socks5://127.0.0.1:51001", 30.0):
        pass
    new_ms = (time.perf_counter() - t0) * 1000
    return old_ms, new_ms


def main():
    r_old, r_new = bench_resolve_spike()
    c_old, c_new = bench_connect_gap()

    print("Lingling latency benchmark - explicit model, model think-time excluded")
    print(f"simulated: /models round-trip = {SIMULATED_MODELS_ROUNDTRIP_S * 1000:.0f} ms, "
          f"handshake = {SIMULATED_HANDSHAKE_S * 1000:.0f} ms")
    print(f"refresh window = {config.CATALOG_TTL_SECONDS:.0f} s (one request in it used to spend the spike)\n")

    header = f"{'scenario':34}{'OLD (ms)':>12}{'NEW (ms)':>12}{'saved (ms)':>12}{'speedup':>9}"
    print(header)
    print("-" * len(header))

    def row(label, o, n):
        s = "%.0fx" % (o / max(n, 1e-6))
        print(f"{label:34}{o:>12.1f}{n:>12.1f}{o - n:>12.1f}{s:>9}")

    row("worst request in a refresh window (resolve)", r_old, r_new)
    row("turn after a short idle gap (connect)", c_old, c_new)
    row("worst case combined (expired + cold)", r_old + c_old, r_new + c_new)
    print()
    print("notes: the resolve spike hits ONE request per refresh window; the connect")
    print("handshake hits a turn only when no warm tunnel is available. Steady-state")
    print("requests (cached + warm) are effectively instant on both versions.")


if __name__ == "__main__":
    main()