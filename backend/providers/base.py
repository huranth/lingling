"""Provider abstraction.

Lingling aggregates many free-tier gateways behind one router. Each gateway is
a :class:`Provider`: it knows its base URL, holds a credential pool, can list
its models dynamically, and can run a chat completion. Adding a new free
provider is one small subclass.

Both OpenCode and any future OpenAI-compatible gateway speak the OpenAI wire
protocol, so the shared :class:`OpenAICompatibleProvider` implements the
transport (POST ``{base}/chat/completions`` with a bearer credential, streaming
and not) and a
default ``GET /models`` reader. A concrete provider supplies its base URL, how
to read its live model list (auth if needed), and how to decide which of its
models are free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
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
        # Keyless providers (OpenCode Zen free tier) send NO Authorization
        # header; a credential is attached only when one is actually
        # configured -- the same "Bearer only if present" rule OmniRoute
        # uses when forwarding upstream auth.
        headers = {"Content-Type": "application/json"}
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

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    def chat_completions(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        proxy_id: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        for k, v in params.items():
            if v is not None:
                body[k] = v
        timeout_val = float(timeout or config.REQUEST_TIMEOUT)

        # Reuse pooled clients so a repeat request to the same proxy skips the
        # SOCKS5 handshake + connection setup that dominate first-token latency.
        from providers.connection_pool import get_connection_pool
        pool = get_connection_pool()
        if proxy_id is None and proxy_url:
            proxy_id = proxy_url.split("://")[-1].replace(":", "_")[:32]
        elif proxy_id is None:
            proxy_id = "_direct_"

        try:
            with pool.get_client(proxy_id, proxy_url, timeout_val) as client:
                resp = client.post(self._url(), json=body, headers=self.auth_headers(secret))
        except httpx.HTTPError as exc:
            raise UpstreamError(504, str(exc), self.id) from exc
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text, self.id)
        return resp.json()

    def stream_chat(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        proxy_id: Optional[str] = None,
        **params: Any,
    ) -> Generator[bytes, None, None]:
        """Stream a chat completion with connection pooling for speed.
        
        OPTIMIZED: Uses pooled httpx clients to avoid SOCKS5 handshake on every request.
        """
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        for k, v in params.items():
            if v is not None:
                body[k] = v
        
        timeout_val = float(timeout or config.REQUEST_TIMEOUT)
        
        # Use connection pool for faster subsequent requests
        from providers.connection_pool import get_connection_pool
        pool = get_connection_pool()
        
        # Generate proxy_id if not provided
        if proxy_id is None and proxy_url:
            proxy_id = proxy_url.split("://")[-1].replace(":", "_")[:32]
        elif proxy_id is None:
            proxy_id = "_direct_"
        
        with pool.get_client(proxy_id, proxy_url, timeout_val) as client:
            try:
                with client.stream(
                    "POST", self._url(), json=body, headers=self.auth_headers(secret)
                ) as resp:
                    if resp.status_code >= 400:
                        detail = resp.read().decode("utf-8", "replace")
                        raise UpstreamError(resp.status_code, detail, self.id)
                    for line in resp.iter_lines():
                        if line:
                            yield line.encode("utf-8") if isinstance(line, str) else line
            except httpx.HTTPError as exc:
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
