"""Pooled httpx clients so repeat requests skip the SOCKS5 handshake.

A fresh client per request paid a SOCKS5 handshake, DNS lookup and TCP connect
each time (~50-200ms). Pooled clients reuse a warm connection per proxy; the
pick is O(1) and the lock is taken only when the pool structure changes.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Optional
from collections import deque

import httpx

from core import config


@dataclass
class PooledClient:
    """A warm httpx client tied to a specific proxy."""
    client: httpx.Client
    proxy_id: str
    proxy_url: str
    last_used: float = 0.0
    request_count: int = 0
    created_at: float = field(default_factory=time.time)
    
    def is_healthy(self) -> bool:
        """Check if the underlying connection is still usable."""
        try:
            return self.client.is_closed is False
        except Exception:
            return False
    
    def touch(self) -> None:
        """Mark this client as recently used."""
        self.last_used = time.time()
        self.request_count += 1


class ConnectionPool:
    """Pool of warm httpx clients for fast request dispatch.
    
    Each proxy URL gets its own pool of clients. When a request comes in,
    we hand out the least-recently-used client for that proxy, avoiding
    connection establishment overhead.
    
    Thread-safe: uses lock only for mutations, reads are lock-free.
    """
    
    def __init__(
        self,
        max_clients_per_proxy: int = 4,
        client_idle_timeout_s: float = 60.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self._max_per_proxy = max_clients_per_proxy
        self._idle_timeout = client_idle_timeout_s
        self._connect_timeout = connect_timeout
        self._lock = threading.Lock()
        self._clients: Dict[str, deque] = {}
    
    def _create_client(self, proxy_url: Optional[str], timeout: float) -> httpx.Client:
        """Create a new httpx client configured for the proxy."""
        timeout_cfg = httpx.Timeout(
            timeout,
            connect=min(self._connect_timeout, timeout),
        )
        kwargs: Dict[str, Any] = {
            "timeout": timeout_cfg,
            "trust_env": False,
            "limits": httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                # Keep the client's own keepalive in step with the pool's prune
                # window so a warm tunnel stays reusable for as long as the pool
                # claims it is.
                keepalive_expiry=config.CONNECTION_POOL_IDLE_S,
            ),
        }
        # HTTP/2 is a speed win but needs the optional h2 package; enable it only
        # when installed, else fall back to HTTP/1.1.
        try:
            import h2  # noqa: F401
            kwargs["http2"] = True
        except ImportError:
            pass
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.Client(**kwargs)
    
    def _get_pool(self, proxy_id: str) -> deque:
        """Get or create the client pool for a proxy."""
        if proxy_id not in self._clients:
            with self._lock:
                if proxy_id not in self._clients:
                    self._clients[proxy_id] = deque(maxlen=self._max_per_proxy)
        return self._clients[proxy_id]
    
    @contextmanager
    def get_client(
        self,
        proxy_id: str,
        proxy_url: Optional[str],
        timeout: float,
    ) -> Generator[httpx.Client, None, None]:
        """Get a warm client from the pool, return it when done."""
        pool = self._get_pool(proxy_id)
        client: Optional[httpx.Client] = None
        pooled: Optional[PooledClient] = None
        
        # Fast path: try to get existing client without lock
        try:
            if pool:
                pooled = pool.pop()
                if pooled and pooled.is_healthy():
                    client = pooled.client
        except (IndexError, KeyError):
            pooled = None
            client = None
        
        # Close unhealthy client
        if pooled and not pooled.is_healthy():
            try:
                pooled.client.close()
            except Exception:
                pass
            pooled = None
            client = None
        
        # Create new client if needed
        if client is None:
            client = self._create_client(proxy_url, timeout)
            pooled = PooledClient(
                client=client,
                proxy_id=proxy_id,
                proxy_url=proxy_url or "",
            )
        
        try:
            pooled.touch()
            yield client
        finally:
            # Return client to pool. The capacity check and the append (or, on
            # overflow, the deliberate close) must run under the same lock the
            # grab path already takes; without it two threads releasing against
            # the same proxy can both pass ``len(pool) < self._max_per_proxy`` and
            # both append, and deque(maxlen=N) would then silently drop the
            # oldest PooledClient -- without calling ``.close()`` -- leaking its
            # httpx client (with held-socket fds). Holding the lock also repairs
            # the previously-incomplete branch where ``is_healthy() == False``
            # with capacity left in the pool did nothing at all: the un-healthy
            # client was abandoned and never closed.
            with self._lock:
                if pooled.is_healthy() and len(pool) < self._max_per_proxy:
                    pool.append(pooled)
                else:
                    try:
                        pooled.client.close()
                    except Exception:
                        pass
    
    def invalidate(self, proxy_id: str) -> None:
        """Close all clients for a proxy."""
        with self._lock:
            pool = self._clients.pop(proxy_id, None)
            if pool:
                for pooled in pool:
                    try:
                        pooled.client.close()
                    except Exception:
                        pass
    
    def prune_idle(self, max_idle_s: float = 60.0) -> int:
        """Remove clients idle longer than max_idle_s."""
        pruned = 0
        now = time.time()
        with self._lock:
            for proxy_id, pool in list(self._clients.items()):
                to_remove = []
                for pooled in pool:
                    if (now - pooled.last_used) > max_idle_s:
                        to_remove.append(pooled)
                for pooled in to_remove:
                    try:
                        pool.remove(pooled)
                        pooled.client.close()
                        pruned += 1
                    except Exception:
                        pass
        return pruned
    
    def shutdown(self) -> None:
        """Close all pooled clients."""
        with self._lock:
            for pool in self._clients.values():
                for pooled in pool:
                    try:
                        pooled.client.close()
                    except Exception:
                        pass
            self._clients.clear()
    
    def stats(self) -> Dict[str, Any]:
        """Return pool statistics."""
        total_clients = 0
        per_proxy: Dict[str, int] = {}
        for proxy_id, pool in self._clients.items():
            count = len(pool)
            total_clients += count
            per_proxy[proxy_id] = count
        return {"total_clients": total_clients, "proxies": per_proxy}


# Global connection pool singleton
_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def get_connection_pool() -> ConnectionPool:
    """Get or create the global connection pool."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    max_clients_per_proxy=config.CONNECTION_POOL_MAX_PER_PROXY,
                    client_idle_timeout_s=config.CONNECTION_POOL_IDLE_S,
                    connect_timeout=config.PROXY_CONNECT_TIMEOUT,
                )
                import atexit
                atexit.register(_pool.shutdown)
    return _pool