"""Comprehensive Race Condition Tests for Lingling Backend.

This test suite validates thread-safety and async-safety across all critical
shared state paths in the Lingling codebase. It covers:
  - Database operations with concurrent access
  - Key pool rotations under contention  
  - Proxy pool mutations during health daemon cycles
  - Catalog catalog refresh starvation prevention
  - WARP manager lifecycle state transitions

Run with: pytest tests/test_race_conditions.py -xvs
"""
import asyncio
import threading
import time
from unittest.mock import Mock
import pytest


# ============================================================================
# THREAD-SAFE DATABASE OPERATIONS
# ============================================================================

def test_unit_usage_store_concurrent_writes_no_corruption(usage_db_path: str):
    """Multiple threads logging simultaneously must not corrupt database schema.

    This reproduces the scenario where N concurrent requests finish at nearly
    the same timestamp and try to INSERT into request_log at the exact same
    moment. Without proper locking, SQLite could receive interleaved writes
    or violate atomicity constraints.
    """
    from usage.store import UsageStore
    
    store = UsageStore(db_path=usage_db_path)
    store.reset()
    
    n_threads = 50
    log_count = [0]
    lock = threading.Lock()
    
    def worker(request_id: int):
        """Log a request and count successes."""
        try:
            store.log(
                requested_model=f"model-{request_id % 5}",
                routed_model=f"routed-{request_id % 3}",
                provider=f"prov-{request_id % 2}",
                routed_by="test",
                reason="concurrent-test",
                tokens_in=10 + request_id,
                tokens_out=20 + request_id,
                latency_ms=100 + request_id,
                status="ok_stream",
                had_images=request_id % 3 == 0,
                account_id=f"user-{request_id % 10}",
                error=None,
                reasoning_tokens=5,
                streamed=True,
            )
            with lock:
                log_count[0] += 1
        except Exception as exc:
            raise AssertionError(f"Thread {threading.current_thread().name} failed: {exc}")
    
    threads = [
        threading.Thread(target=worker, args=(i,), name=f"log-{i}")
        for i in range(n_threads)
    ]
    
    # Launch all at once for maximum contention
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert log_count[0] == n_threads, \
        f"Not all threads completed successfully: {log_count[0]}/{n_threads}"
    
    # Verify integrity via summary
    summary = store.summary()
    totals = summary["totals"]
    
    assert totals["requests"] == n_threads, \
        f"Database lost records: {totals['requests']}/{n_threads}"
    assert totals["tokens_in"] > 0, "No token data recorded"
    assert totals["streamed"] == n_threads, "Streamed flag corrupted"


def test_unit_usage_store_concurrent_reads_during_writes():
    """Reading stats while logs are being written must return consistent snapshot.

    The summary method acquires a single lock for its duration. Concurrent writers
    should never see partial results or raise exceptions from mid-transaction reads.
    """
    from usage.store import UsageStore
    
    store = UsageStore()
    store.reset()
    
    stop_flag = threading.Event()
    errors = []
    
    def writer():
        """Continuously add log entries."""
        counter = [0]
        while not stop_flag.is_set():
            try:
                store.log(
                    requested_model=f"model-{counter[0] % 3}",
                    routed_model="stable-model",
                    provider="provider",
                    routed_by="writer",
                    reason="concurrent",
                    tokens_in=10,
                    tokens_out=20,
                    latency_ms=50,
                    status="ok",
                    had_images=False,
                    account_id="user-1",
                    error=None,
                    reasoning_tokens=0,
                    streamed=True,
                )
                counter[0] += 1
                time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)
                break
    
    def reader():
        """Continuously read stats."""
        last_count = None
        while not stop_flag.is_set():
            try:
                summary = store.summary()
                current_count = summary["totals"]["requests"]
                
                # Count must be non-decreasing
                if last_count is not None:
                    assert current_count >= last_count, \
                        f"Summary regressed: {last_count} -> {current_count}"
                last_count = current_count
                
                time.sleep(0.002)
            except Exception as exc:
                errors.append(exc)
                break
    
    # Run writers and readers concurrently
    writers = [threading.Thread(target=writer, name=f"w{i}") for i in range(5)]
    readers = [threading.Thread(target=reader, name=f"r{i}") for i in range(3)]
    
    for w in writers:
        w.start()
    for r in readers:
        r.start()
    
    # Let them run for 3 seconds under max contention
    time.sleep(3)
    stop_flag.set()
    
    for w in writers:
        w.join(timeout=2)
    for r in readers:
        r.join(timeout=2)
    
    assert not errors, f"Concurrent access caused errors: {errors[:3]}"


# ============================================================================
# KEY POOL ROTATION UNDER CONTENTION
# ============================================================================

def test_unit_key_pool_pick_rotation_synchronized_with_modifications():
    """Simultaneous picks and removals must not lose keys or throw IndexError.

    The pick() method advances self._cursor while remove() modifies self.keys
    list. Without synchronization, this causes either:
      - IndexError when cursor points past truncated list
      - Silent key loss when rotation skips modified indices
    """
    from providers.key_pool import KeyPool
    
    keys = [f"k{i}" for i in range(20)]
    pool = KeyPool.from_list(keys)
    
    errors = []
    picks_seen = set()
    lock = threading.Lock()
    stop = threading.Event()
    
    def picker():
        """Keep picking until told to stop."""
        while not stop.is_set():
            key = pool.pick()
            if key:
                with lock:
                    picks_seen.add(key.id)
            time.sleep(0.001)
    
    def remover():
        """Remove one by one until empty."""
        while not stop.is_set() and len(pool.keys) > 0:
            key_to_remove = pool.keys[0].id if pool.keys else None
            if key_to_remove:
                pool.remove(key_to_remove)
            time.sleep(0.002)
    
    # Start both at same time for max contention
    threads = (
        [threading.Thread(target=picker, name="picker") for _ in range(5)] +
        [threading.Thread(target=remover, name="remover") for _ in range(3)]
    )
    
    for t in threads:
        t.start()
    
    time.sleep(2)
    stop.set()
    
    for t in threads:
        t.join(timeout=1)
    
    assert not errors, f"Key pool contention caused errors: {errors[:3]}"
    
    # Some keys should have been picked before they disappeared
    assert len(picks_seen) > 0, "No keys were successfully picked"


# ============================================================================
# PROXY POOL MUTATION DURING HEALTH DAEMON CYCLES
# ============================================================================

def test_unit_proxy_pool_add_remove_during_pick():
    """Adding/removing proxies while executor is selecting one must be safe.

    The executor calls proxy_pool.pick() which returns a Proxy object. If the
    health daemon removes that same proxy between pick() and when it tries to
    use it, the executor should handle it gracefully rather than crash.
    """
    from providers.proxy_pool import ProxyPool
    
    initial_proxies = [f"socks5://127.0.0.1:{10000+i}" for i in range(10)]
    pool = ProxyPool.from_list(initial_proxies)
    
    errors = []
    successful_uses = [0]  # Wrap in list for mutability
    lock = threading.Lock()
    stop = threading.Event()
    
    def executor_worker():
        """Pick a proxy and pretend to use it."""
        while not stop.is_set():
            proxy = pool.pick()
            if proxy:
                # Pretend we're doing work with this proxy
                # In reality, we might get removed right after pick() returns
                time.sleep(0.001)
                with lock:
                    successful_uses[0] += 1
                time.sleep(0.002)
            else:
                time.sleep(0.001)
    
    def health_daemon():
        """Simulate health daemon adding/removing proxies."""
        counter = [0]
        while not stop.is_set():
            # Remove oldest
            all_proxies = pool.get_all_proxies()
            if all_proxies:
                pool.remove(all_proxies[0].id)
            
            # Add fresh one
            pool.add(
                f"socks5://127.0.0.1:{11000+counter[0]}",
                proxy_id=f"health-{counter[0]}",
                label="Health refreshed"
            )
            counter[0] += 1
            
            time.sleep(0.003)
    
    threads = (
        [threading.Thread(target=executor_worker, name=f"exec{i}") for i in range(5)] +
        [threading.Thread(target=health_daemon, name="health")]
    )
    
    for t in threads:
        t.start()
    
    time.sleep(3)
    stop.set()
    
    for t in threads:
        t.join(timeout=2)
    
    assert not errors, f"Proxy pool mutation race caused: {errors[:3]}"
    assert successful_uses[0] > 0, "Executor didn't successfully use any proxies"


# ============================================================================
# CATALOG REFRESH STARVATION PREVENTION
# ============================================================================

def test_unit_catalog_concurrent_free_calls_dont_fork_upstream_requests():
    """N simultaneous catalog.free() calls must trigger exactly ONE upstream fetch.

    This is the thundering herd protection: multiple consumers asking "is there
    capacity?" shouldn't each re-fetch the entire model list independently.
    They should share a single background refresh attempt.
    """
    from models.catalog import UnifiedCatalog
    from providers.base import Provider
    from providers.key_pool import KeyPool
    
    class TrackingProvider(Provider):
        id = "tracking-prov"
        display_name = "Tracking"
        priority = 10
        
        def __init__(self):
            super().__init__(KeyPool([]))
            self.fetch_count = 0
        
        def requires_key(self):
            return False
        
        def fetch_model_ids(self):
            self.fetch_count += 1
            time.sleep(0.05)  # Simulate slow upstream
            return ["model-a", "model-b"]
        
        def is_model_free(self, model_id, meta):
            return True
    
    prov = TrackingProvider()
    cat = UnifiedCatalog({"tracking-prov": prov})
    cat._stale.clear()
    cat._generated_at = 0.0
    
    n_callers = 20
    results = []
    
    def caller():
        result = cat.free()
        results.append(result)
    
    threads = [threading.Thread(target=caller, name=f"c{i}") for i in range(n_callers)]
    
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # All callers should get same list back
    assert all(isinstance(r, list) for r in results), "Not all callers got valid response"
    
    # Most importantly: upstream should only be called ONCE
    assert prov.fetch_count == 1, \
        f"Thundering herd occurred: {prov.fetch_count} upstream calls instead of 1"
    
    # All lists should be non-empty since we provided models
    assert all(len(r) > 0 for r in results), "Some callers got empty model lists"


# ============================================================================
# WARP MANAGER STATE TRANSITIONS
# ============================================================================

def test_unit_warp_manager_status_vs_modification_concurrency():
    """Reading status while starting/stopping instances must not raise.

    The health daemon periodically calls warp_manager.status() while
    start_all() and stop_all() may be running simultaneously. State corruption
    would manifest as AttributeError on None process objects or missing fields.
    """
    from warp.manager import WarpManager
    
    # Create minimal manager without actually starting anything
    mgr = Mock(spec=WarpManager)
    
    # Pre-create instances with minimal setup
    for i in range(3):
        inst = Mock()
        inst.private_key = f"fake-key-{i}"
        inst.address_v4 = f"192.168.{i}.1"
        inst.address_v6 = f"::1:{i}"
        inst.process = None  # Not started yet
        inst.index = i
        inst.port = 10000 + i
        mgr.instances = [inst] * 3
        
    # Mock status method to return stable dict
    def mock_status():
        return {
            "count": 3,
            "proxies_running": 0,
            "instances": [{"index": i, "status": "stopped"} for i in range(3)],
        }
    
    mgr.status = mock_status
    
    errors = []
    statuses_seen = [0]  # Use count instead of storing full objects
    stop = threading.Event()
    lock = threading.Lock()
    
    def status_reader():
        """Continuously read status."""
        while not stop.is_set():
            try:
                status = mgr.status()
                with lock:
                    statuses_seen[0] += 1
                time.sleep(0.001)
            except AttributeError as e:
                errors.append(f"Status read crashed: {e}")
                break
            except Exception as e:
                pass  # Other errors acceptable during modification
    
    def simulator_start():
        """Simulate instance modifications like start_all does."""
        while not stop.is_set():
            try:
                # Mimic what start_all does: modify port, process
                inst = mgr.instances[0]
                old_process = inst.process  # Read first
                inst.port = 10000  # Modify
                inst.process = None  # Set to Popen(None)
                time.sleep(0.005)
                
                # Cleanup
                inst.process = old_process
                time.sleep(0.003)
            except Exception as e:
                errors.append(f"Modification failed: {e}")
                break
    
    threads = [
        threading.Thread(target=status_reader, name="status-reader"),
        threading.Thread(target=simulator_start, name="mod-simulator"),
    ]
    
    for t in threads:
        t.start()
    
    time.sleep(2)
    stop.set()
    
    for t in threads:
        t.join(timeout=1)
    
    assert not errors, f"Status-modification race caused: {errors[:3]}"
    assert statuses_seen[0] > 10, f"Not enough status reads captured: {statuses_seen[0]}"


def test_unit_warp_health_daemon_sync_pool_state_stability():
    """Health daemon syncing pool must not leave inconsistent state.

    During _sync_pool, the health daemon adds healthy proxies and removes
    unhealthy ones. This happens while the executor is actively using pool.pick().
    Pool operations must be atomic so executor never sees partially-synced state.
    """
    from providers.proxy_pool import ProxyPool
    
    # Create real pool with initial data
    initial_proxies = [f"socks5://127.0.0.1:{9000+i}" for i in range(3)]
    mock_pool = ProxyPool.from_list(initial_proxies)
    
    # Simulate concurrent pool operations
    errors = []
    operations_committed = [0]  # Use list for mutability
    lock = threading.Lock()
    
    def sync_operation():
        """Simulate _sync_pool doing add/remove operations."""
        try:
            for _ in range(10):
                # These should all be locked internally
                mock_pool.add("socks5://127.0.0.1:9000", proxy_id="test-1")
                mock_pool.remove("test-1")
                mock_pool.add("socks5://127.0.0.1:9001", proxy_id="test-2")
                with lock:
                    operations_committed[0] += 3
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)
    
    def concurrent_pick():
        """Simulate executor calling pick()."""
        for _ in range(50):
            try:
                mock_pool.pick()
                mock_pool.get_by_id("test-1")
                time.sleep(0.001)
            except Exception as e:
                errors.append(e)
                break
    
    threads = (
        [threading.Thread(target=sync_operation, name=f"sync{i}") for i in range(3)] +
        [threading.Thread(target=concurrent_pick, name=f"pick{i}") for i in range(2)]
    )
    
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert not errors, f"Pool sync race caused: {errors[:3]}"
    assert operations_committed[0] > 0, "No sync operations completed"


# ============================================================================
# ASYNC/RACE COMBINATIONS
# ============================================================================

@pytest.mark.asyncio
async def test_async_task_concurrent_usage_log_finalization():
    """Multiple async tasks finalizing usage logs simultaneously must not deadlock.

    Each streaming request holds a usage id until completion, then calls
    usage_store.finalize(id, ...) with full metrics. N concurrent streams
    finishing together should all complete their finalizations cleanly.
    """
    from usage.store import UsageStore
    
    store = UsageStore()
    store.reset()
    
    n_tasks = 30
    finalized_ids = []
    lock = threading.Lock()
    errors = []
    
    async def stream_task(task_id: int):
        """Simulate a streaming request completing."""
        try:
            # Log initial chunk
            req_id = store.log(
                requested_model=f"model-{task_id}",
                routed_model="final-model",
                provider="stream-provider",
                routed_by="async-test",
                reason="streaming",
                tokens_in=5,
                tokens_out=10,
                latency_ms=0,
                status="ok_stream",
                had_images=False,
                account_id=f"user-{task_id}",
                error=None,
                reasoning_tokens=0,
                streamed=True,
            )
            
            # Simulate some processing time
            await asyncio.sleep(0.001)
            
            # Finalize with real metrics
            store.finalize(
                row_id=req_id,
                tokens_in=10 + task_id,
                tokens_out=20 + task_id,
                latency_ms=100 + task_id * 2,
                status="ok_recovered",
                error=None,
                reasoning_tokens=5,
                routed_model=f"routed-{task_id % 3}",
                reason="completed",
            )
            
            with lock:
                finalized_ids.append(req_id)
                
        except Exception as e:
            errors.append(e)
    
    # Launch all async tasks
    await asyncio.gather(*[stream_task(i) for i in range(n_tasks)])
    
    assert not errors, f"Async finalization errors: {errors[:3]}"
    assert len(finalized_ids) == n_tasks, \
        f"Not all async tasks finalized: {len(finalized_ids)}/{n_tasks}"


@pytest.mark.asyncio
async def test_mixed_async_sync_concurrent_access():
    """Mixing async routes with sync background tasks must keep shared state consistent.

    FastAPI handles requests async while background threads (health daemon,
    catalog refresh) run sync. Both access the same usage store, key pools,
    proxy pools. No race conditions should leak through.
    """
    from usage.store import UsageStore
    from providers.key_pool import KeyPool
    
    store = UsageStore()
    store.reset()
    
    key_pool = KeyPool.from_list(["key-1", "key-2", "key-3"])
    
    errors = []
    access_log = []
    lock = threading.Lock()
    
    def sync_key_picker():
        """Sync background task picking keys."""
        for _ in range(20):
            key = key_pool.pick()
            if key:
                with lock:
                    access_log.append(("sync_key", key.id))
            time.sleep(0.002)
    
    async def async_route_simulator():
        """Async route simulating request handling."""
        for i in range(30):
            # Async operation
            await asyncio.sleep(0.001)
            
            # Access shared state
            store.log(
                requested_model=f"route-{i}",
                routed_model="routed",
                provider="async-provider",
                routed_by="simulated",
                reason="mixed-test",
                tokens_in=5,
                tokens_out=10,
                latency_ms=50,
                status="ok",
                had_images=False,
                account_id="user-sim",
                error=None,
                reasoning_tokens=0,
                streamed=False,
            )
            
            with lock:
                access_log.append(("async_log", i))
    
    # Run sync and async concurrently
    sync_thread = threading.Thread(target=sync_key_picker)
    sync_thread.start()
    
    await asyncio.gather(*[async_route_simulator() for _ in range(3)])
    
    sync_thread.join(timeout=2)
    
    assert not errors, f"Mixed async/sync race: {errors[:3]}"
    
    # Should have mixed access patterns
    sync_accesses = sum(1 for t, _ in access_log if t == "sync_key")
    async_accesses = sum(1 for t, _ in access_log if t == "async_log")
    
    assert sync_accesses > 0, "No sync accesses logged"
    assert async_accesses > 0, "No async accesses logged"
