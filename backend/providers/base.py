"""Provider abstraction.

Lingling aggregates many free-tier gateways behind one router. Each gateway is a
:class:`Provider`: it knows its base URL, holds a credential pool, can list its
models dynamically, and can run a chat completion. Adding a new free provider is
one small subclass.

Both OpenCode and any future OpenAI-compatible gateway speak the OpenAI wire
protocol, so the shared :class:`OpenAICompatibleProvider` implements the
transport (POST ``{base}/chat/completions`` with a bearer credential, streaming
and not) and a
default ``GET /models`` reader. A concrete provider supplies its base URL, how
to read its live model list (auth if needed), and how to decide which of its
models are free.

Connection pooling
------------------
``OpenAICompatibleProvider`` keeps one ``httpx.Client`` per egress proxy URL
(bucketed under ``"<DIRECT>"`` for proxy-less calls) so back-to-back requests
through the same SOCKS5 tunnel reuse TCP+TLS to upstream. A fresh
``httpx.Client`` per request paid the full handshake on every call -- roughly
300-800ms per chat request, repeated for every Codex turn and every led
session. The pool lets a healthy proxy carry dozens of requests in a second
without re-handshaking. ``trust_env=False`` keeps process-wide HTTP(S)_PROXY
from hijacking the cached client across restarts; clients are closed on
process shutdown via :meth:`close`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
import threading
from typing import Any, Dict, Generator, List, Optional

import httpx

from core import config
from models import metadata
from providers.key_pool import KeyPool


class UpstreamError(Exception):
    """Raised when an upstream provider returns a non-success status."""

    def __init__(self, status_code: int, detail: str = "", provider_id: str = "") -> None:
        super().__init__(f"[{provider_id}] upstream HTTP {status_code}: {detail[:300]}")
        self.status_code = status_code
        self.detail = detail
        self.provider_id = provider_id


def _prettify(model_id: str) -> str:
    """``deepseek-v4-flash-free`` -> ``Deepseek V4 Flash Free`` (prefix stripped)."""
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    return " ".join(p.capitalize() for p in bare.replace("_", "-").split("-"))


@dataclass
class ProviderModel:
    """A model offered by one provider, with the capabilities routing needs."""

    id: str                       # provider-local model id
    provider_id: str
    name: str
    free: bool
    vision: bool
    reasoning: bool
    context_length: Optional[int]
    max_output: Optional[int]
    modalities: List[str] = field(default_factory=list)
    # Optional curated capability notes from the provider, e.g. a human-readable
    # `desc` of strengths. The routing dispatcher reads `desc` verbatim so it
    # routes on real capabilities rather than guessing from the model id.
    capabilities: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Provider(ABC):
    """Interface every free-tier gateway implements."""

    id: str = "provider"
    display_name: str = "Provider"
    base_url: str = ""
    priority: int = 100          # lower = preferred when several serve a model
    # When True, every model this provider lists is treated as free.
    all_free: bool = False

    def __init__(self, keys: KeyPool) -> None:
        self.keys = keys
        # Whether the most recent list_models() fetch succeeded. The catalog
        # reads this to keep serving the last good model list through a
        # transient fetch failure instead of blanking out (see UnifiedCatalog).
        self.last_fetch_ok: bool = True

    # -- configuration -----------------------------------------------------
    def requires_key(self) -> bool:
        return True

    def needs_proxy(self) -> bool:
        """Whether this provider's requests should be routed through the egress
        proxy pool. Override to True for IP-rate-limited keyless providers
        (OpenCode free tier).
        """
        return False

    def prefer_direct(self, model_id: str) -> bool:
        """Whether latency-sensitive requests for *model_id* bypass egress proxies.

        This is deliberately opt-in at provider level: a provider that requires
        a proxy for connectivity can retain its normal proxy behavior.
        """
        return False

    def is_configured(self) -> bool:
        if not self.requires_key():
            return True
        return len(self.keys) > 0

    # -- models ------------------------------------------------------------
    @abstractmethod
    def fetch_model_ids(self) -> List[str]:
        """Return the live list of model ids this provider exposes."""

    @abstractmethod
    def is_model_free(self, model_id: str, meta: Dict[str, Any]) -> bool:
        """Decide whether a given model is free for this provider."""

    def build_model(self, model_id: str) -> ProviderModel:
        """Enrich a raw model id into a capability-annotated ProviderModel."""
        e = metadata.enrich(model_id, prefer_provider=self.id)
        meta = e.get("_meta", {})
        return ProviderModel(
            id=model_id,
            provider_id=self.id,
            name=e.get("name") or _prettify(model_id),
            free=self.is_model_free(model_id, meta),
            vision=e.get("vision", False),
            reasoning=e.get("reasoning", False),
            context_length=e.get("context_length"),
            max_output=e.get("max_output"),
            modalities=e.get("modalities", []),
        )

    def list_models(self) -> List[ProviderModel]:
        """Fetch live ids and build enriched models.

        Returns ``[]`` on any failure (provider unconfigured or unreachable) so
        one provider never breaks the whole catalog. ``last_fetch_ok`` records
        whether the fetch actually succeeded, so the catalog can distinguish
        "this provider genuinely has no models" from "the /models call just
        failed" and avoid discarding a good cached list on a transient blip.
        """
        try:
            ids = self.fetch_model_ids()
        except Exception:
            self.last_fetch_ok = False
            return []
        self.last_fetch_ok = True
        return [self.build_model(mid) for mid in ids]

    # -- transport ---------------------------------------------------------
    def auth_headers(self, secret: str) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Honest User-Agent: the gateway identifies as Lingling, not spoofing
            # opencode CLI. Docs claimed opencode/1.0 was required for free tier;
            # live probes show it doesn't affect tier (both 200), so we keep it real.
            "User-Agent": "lingling/0.2.0",
        }
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def chat_completions(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, **params: Any,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def stream_chat(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, **params: Any,
    ) -> Generator[bytes, None, None]:
        raise NotImplementedError

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "priority": self.priority,
            "configured": self.is_configured(),
            "requires_key": self.requires_key(),
            "all_free": self.all_free,
            "keys": self.keys.status(),
        }


class OpenAICompatibleProvider(Provider):
    """Shared transport for providers that speak the OpenAI wire protocol."""

    # keepalive limits per cached client. A session-heavy Codex run must not
    # accumulate hundreds of idle keepalive sockets to a single egress IP;
    # 10 idle / 20 total caps the per-(proxy_url) footprint while still
    # amortising ~all of the per-request handshake on warm paths.
    _POOL_KEEPALIVE = 10
    _POOL_MAX_CONNS = 20
    _POOL_KEEPALIVE_EXPIRY_S = 60.0

    def __init__(self, keys: KeyPool) -> None:
        super().__init__(keys)
        # proxy_url (or "<DIRECT>") -> httpx.Client. Lazy-built; one client per
        # tunnel so back-to-back requests reuse the TCP+TLS handshake. A
        # separate threading.Lock guards creation (httpx.Client is itself
        # thread-safe; the lock protects the dict + serialises the cold build).
        self._clients: Dict[str, httpx.Client] = {}
        self._clients_lock = threading.Lock()

    @classmethod
    def _new_client(
        cls, proxy_url: Optional[str], timeout: Optional[int],
    ) -> httpx.Client:
        """Build an httpx.Client with the right transport/timeout config."""
        # The timeout budget is split into fields so the *read* ceiling is no
        # longer the ``STREAM_FIRST_TOKEN_TIMEOUT`` that callers pass in as
        # ``timeout`` for streaming. When ``timeout`` was applied as a single
        # scalar, every inter-chunk gap on a live stream inherited the 30s
        # first-token budget -- a thinking model pausing past it raised
        # ``ReadTimeout`` -> ``UpstreamError(504, "read operation timed out")``
        # -> the mid-flight break + ``lingling_reset`` seen in the live logs.
        # The first-token budget is enforced separately (see
        # ``executor.execute_stream``); here the client's *default* read stays
        # at ``REQUEST_TIMEOUT`` for non-stream, and each stream applies its own
        # per-request override via :meth:`_stream_timeout`.
        base = float(timeout or config.REQUEST_TIMEOUT)
        connect = min(config.PROXY_CONNECT_TIMEOUT, base) if proxy_url else base
        kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(
                connect=connect,
                read=float(config.REQUEST_TIMEOUT),
                write=base,
                pool=base,
            ),
            # Never silently inherit HTTP(S)_PROXY/ALL_PROXY from the process;
            # explicit proxy_url is supported via the pool only.
            "trust_env": False,
            "limits": httpx.Limits(
                max_keepalive_connections=cls._POOL_KEEPALIVE,
                max_connections=cls._POOL_MAX_CONNS,
                keepalive_expiry=cls._POOL_KEEPALIVE_EXPIRY_S,
            ),
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.Client(**kwargs)

    def _client_for(
        self, proxy_url: Optional[str], timeout: Optional[int],
    ) -> httpx.Client:
        """Return the cached client for *proxy_url*, building one if needed.

        The dict lookup is the hot path -- the lock is contention-free on
        warm hits; cold creates are serialised so two simultaneous first
        requests don't both build the same client.
        """
        key = proxy_url or "<DIRECT>"
        client = self._clients.get(key)
        if client is not None:
            return client
        with self._clients_lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            client = self._new_client(proxy_url, timeout)
            self._clients[key] = client
            return client

    def _stream_timeout(
        self, proxy_url: Optional[str], timeout: Optional[float],
    ) -> httpx.Timeout:
        """Per-request timeout for a *live* stream: short connect, long read.

        ``read`` is the socket-read ceiling for every inter-chunk gap once the
        first chunk has arrived. It must sit *above* the ``stream_idle``
        watchdog budget so a genuine thinking pause is left to the watchdog
        (which counts usable frames and ignores SSE keepalives) instead of
        being misread by httpx as ``ReadTimeout`` -> a spurious 504 + reset.
        ``connect`` stays tight so a dead SOCKS port still fails over fast, and
        ``write``/``pool`` are bounded by the caller's budget. See
        ``core.config.STREAM_READ_TIMEOUT`` for the rationale.
        """
        base = float(timeout or config.REQUEST_TIMEOUT)
        connect = min(config.PROXY_CONNECT_TIMEOUT, base) if proxy_url else base
        return httpx.Timeout(
            connect=connect,
            read=config.STREAM_READ_TIMEOUT,
            write=base,
            pool=base,
        )

    def _evict_proxy_client(self, proxy_url: Optional[str]) -> None:
        """Drop the cached client for *proxy_url* and close it.

        Used after a tunnel-level failure (SOCKS5 handshake, mid-stream read
        abort). The next request through that proxy rebuilds a fresh client;
        the old one's keepalive sockets to a possibly-dead tunnel are torn
        down instead of waiting ``keepalive_expiry`` seconds to age out.
        """
        key = proxy_url or "<DIRECT>"
        with self._clients_lock:
            dead = self._clients.pop(key, None)
        if dead is not None:
            try:
                dead.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Close every cached client. Idempotent; safe to call repeatedly."""
        with self._clients_lock:
            clients, self._clients = self._clients, {}
        for c in clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001 -- close is best-effort
                pass

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    def chat_completions(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        for k, v in params.items():
            if v is not None:
                body[k] = v
        client = self._client_for(proxy_url, timeout)
        try:
            resp = client.post(
                self._url(), json=body, headers=self.auth_headers(secret),
            )
        except httpx.HTTPError as exc:
            # Proxy/connect/read failures are upstream availability failures, not
            # application bugs. Normalizing them lets the executor cool the bad
            # proxy and try another egress instead of returning an ASGI 500.
            raise UpstreamError(504, str(exc), self.id) from exc
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text, self.id)
        return resp.json()

    def stream_chat(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Generator[bytes, None, None]:
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        for k, v in params.items():
            if v is not None:
                body[k] = v
        # Reuse the cached client (and its live TCP+TLS) for streaming too;
        # the client lives for the life of the provider, so streaming can
        # span many chunks across a single tunnel without re-handshaking.
        client = self._client_for(proxy_url, timeout)
        try:
            with client.stream(
                "POST", self._url(), json=body, headers=self.auth_headers(secret),
                timeout=self._stream_timeout(proxy_url, timeout),
            ) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", "replace")
                    raise UpstreamError(resp.status_code, detail, self.id)
                for line in resp.iter_lines():
                    if line:
                        yield line.encode("utf-8") if isinstance(line, str) else line
        except httpx.TimeoutException as exc:
            # A read/pool/write timeout is a *stall*, not a broken tunnel. The
            # upstream simply went quiet past the read ceiling; the cached
            # client's sockets are still valid, so we keep them for the next
            # request instead of forcing a fresh TCP+TLS+SOCKS handshake.
            raise UpstreamError(504, str(exc), self.id) from exc
        except httpx.HTTPError as exc:
            # A genuine transport failure (connection refused/reset, protocol
            # error) *did* poison the tunnel. Drop the cached client so the
            # next request through this proxy rebuilds a fresh one rather than
            # waiting out keepalive on dead sockets.
            self._evict_proxy_client(proxy_url)
            raise UpstreamError(504, str(exc), self.id) from exc

    def _models_secret(self) -> Optional[str]:
        """Credential used to read the model list, if the endpoint needs one.

        Keyless providers (OpenCode) return None; token-gated providers override
        this to supply a credential.
        """
        return None

    def fetch_model_ids(self) -> List[str]:
        headers = {"Content-Type": "application/json"}
        secret = self._models_secret()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        with httpx.Client(timeout=config.REQUEST_TIMEOUT, trust_env=False) as client:
            resp = client.get(self._models_url(), headers=headers)
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text, self.id)
        # A non-JSON body (e.g. an HTML auth page) means we are not authenticated.
        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            raise UpstreamError(
                401, "expected a JSON model list but got HTML (missing/invalid credential?)", self.id
            )
        payload = resp.json()
        return [m["id"] for m in payload.get("data", []) if m.get("id")]


def extract_assistant_text(response: Dict[str, Any]) -> str:
    """Pull the assistant text out of a non-streaming response."""
    try:
        choice = response["choices"][0]
        message = choice.get("message") or {}

        # Reasoning models tend to put visible/usable text into reasoning_content
        # and leave content empty. Return reasoning text when content is empty.
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content

        fallback_keys = ("reasoning_content", "reasoning", "thinking")
        for key in fallback_keys:
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        if isinstance(content, list):
            return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    except (KeyError, IndexError, TypeError):
        pass
    return ""


def promote_reasoning_to_content(response: Dict[str, Any]) -> bool:
    """Surface a reasoning-model blank turn into ``message.content`` in place.

    Reasoning-only models (notably nemotron) sometimes return
    ``message.content == ""`` (or ``None``) plus a populated ``reasoning``,
    ``reasoning_content`` or ``thinking`` sibling field. An OpenAI-compatible
    chat client reading ``choices[0].message.content`` would then render a
    blank turn even though the upstream produced text -- the same family of
    blank-turn bug that the Claude Code bridge (``messages_response.py``,
    already fixed) and the Responses bridge (``responses_bridge._assistant_text``,
    already fixed) guard against. The raw chat-completions path returned the
    upstream dict untouched, so it was the lone outlier; this mutates it in
    place when content is blank, copying the first non-empty reasoning text
    into ``content`` -- the reasoning fields are preserved so a client that
    does understand them still gets them.

    Args:
        response: a non-streaming chat-completion dict (mut: message.content).

    Returns:
        True when ``content`` was overwritten, False otherwise (happy path,
        multi-part list content, or no usable reasoning fallback found).
    """
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return False
    if isinstance(content, list):
        # Multi-part content was already structured upstream; do not collapse
        # it into a single string and risk losing parts / annotations.
        return False
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            message["content"] = value
            return True
    return False


def extract_usage(response: Dict[str, Any]) -> Dict[str, int]:
    """Token counts from a non-streaming response.

    ``reasoning_tokens`` is a subset of ``completion_tokens`` (it is not added
    to the total) and is reported separately so the dashboard can show how much
    of the output was thinking.
    """
    usage = response.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "tokens_in": int(usage.get("prompt_tokens", 0) or 0),
        "tokens_out": int(usage.get("completion_tokens", 0) or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens", 0) or 0),
    }
