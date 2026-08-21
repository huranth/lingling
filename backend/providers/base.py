"""Provider abstraction.

Each free-tier gateway is a :class:`Provider`: it knows its base URL, holds a
credential pool, lists its models, and runs chat completions. OpenCode and any
future OpenAI-compatible gateway share :class:`OpenAICompatibleProvider`, which
implements the transport; a concrete provider supplies its base URL, how to read
its model list, and how to decide which models are free.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Generator, List, Optional

import httpx

from core import config
from models import metadata
from providers.key_pool import KeyPool


# Matches any UTF-16 surrogate code point (U+D800..U+DFFF).
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def strip_lone_surrogates(obj: Any) -> Any:
    """Recursively drop unpaired UTF-16 surrogate code points from a body.

    A client that truncates history mid-emoji can send only half of a surrogate
    pair (e.g. ``\\ud83d``). httpx serializes the body with
    ``json.dumps(ensure_ascii=False).encode("utf-8")``, which cannot encode a
    lone surrogate and raises UnicodeEncodeError before any bytes leave -- so the
    request 500s and every retry on the same body fails identically. Whole emoji
    decode to a single non-surrogate code point and are left untouched.
    """
    if isinstance(obj, str):
        # Fast path: most strings have no surrogates at all.
        if _LONE_SURROGATE_RE.search(obj):
            return _LONE_SURROGATE_RE.sub("", obj)
        return obj
    if isinstance(obj, dict):
        return {k: strip_lone_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_lone_surrogates(v) for v in obj]
    return obj


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
        # Whether the most recent list_models() fetch succeeded, so the catalog
        # can keep serving the last good list through a transient fetch failure.
        self.last_fetch_ok: bool = True

    # -- configuration -----------------------------------------------------
    def requires_key(self) -> bool:
        return True

    def needs_proxy(self) -> bool:
        """Whether requests route through the egress proxy pool. True for
        IP-rate-limited keyless providers (OpenCode free tier)."""
        return False

    def prefer_direct(self, model_id: str) -> bool:
        """Whether latency-sensitive requests for *model_id* bypass the pool."""
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

        Returns ``[]`` on any failure so one provider never breaks the catalog.
        ``last_fetch_ok`` records whether the fetch succeeded, so the catalog can
        tell "no models" from "the /models call failed" and keep a good cached
        list through a transient blip.
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
        # Authorization is attached only when a credential is configured
        # (OpenCode's free tier is keyless).
        #
        # Gotcha: the User-Agent is load-bearing. OpenCode gates its premium free
        # models behind the official client's UA -- a `python-httpx/...` request
        # gets an instant FreeUsageLimitError 429 regardless of IP or quota,
        # while the identical request as `opencode/...` returns 200.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": config.UPSTREAM_USER_AGENT,
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
        # A client that truncated history mid-emoji can leave a lone UTF-16
        # surrogate in the messages; UTF-8 can't encode it and httpx would 500
        # on serialization before any bytes leave. Drop the broken halves.
        body = strip_lone_surrogates(body)
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
        """Stream a chat completion, reusing pooled clients for speed."""
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        for k, v in params.items():
            if v is not None:
                body[k] = v
        # See chat_completions: strip lone UTF-16 surrogates so a truncated-emoji
        # message doesn't 500 in httpx's UTF-8 serialization before we connect.
        body = strip_lone_surrogates(body)

        timeout_val = float(timeout or config.REQUEST_TIMEOUT)

        from providers.connection_pool import get_connection_pool
        pool = get_connection_pool()
        if proxy_id is None and proxy_url:
            proxy_id = proxy_url.split("://")[-1].replace(":", "_")[:32]
        elif proxy_id is None:
            proxy_id = "_direct_"

        from providers import active_streams
        # Only proxied requests ride a re-rollable egress (a wireproxy/tor
        # process the healers can tear down); a direct request has no egress to
        # protect, so it is excluded from the registry entirely -- inc/dec are
        # no-ops for a falsy id, and dashboards never see a phantom _direct_.
        egress_id = proxy_id if proxy_url else None
        active_streams.inc(egress_id)
        try:
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
        finally:
            active_streams.dec(egress_id)

    def _models_secret(self) -> Optional[str]:
        """Credential used to read the model list, if the endpoint needs one.

        Keyless providers (OpenCode) return None; token-gated providers override
        this to supply a credential.
        """
        return None

    def fetch_model_ids(self) -> List[str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": config.UPSTREAM_USER_AGENT,
        }
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
