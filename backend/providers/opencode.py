"""OpenCode provider (primary).

OpenCode Zen (https://opencode.ai/zen/v1) is an OpenAI-compatible AI gateway.
Its free tier is fully KEYLESS: both ``GET /models`` and the free models on
``POST /chat/completions`` answer with no credential (verified live). The
web-creatable "OpenCode Zen API key" is only enforced for PAID models, which
Lingling does not serve -- so Lingling talks to OpenCode keyless by default.

If account keys ARE configured (``accounts.json``) the shared key pool still
works, but for the free tier those keys are optional, not required.

A model is free if it carries OpenCode's ``-free`` suffix or models.dev prices
it at zero; anything else is treated as premium and excluded (fail closed).

Curated capabilities: OpenCode's ``/models`` returns only ``{id, object,
created, owned_by}`` (no modalities), and models.dev does not cover several of
OpenCode's in-house free models. ``FREE_MODEL_CAPS`` below is the authoritative,
empirically verified capability map for the free tier -- it is what the routing
dispatcher reads so it routes on real strengths, not guesses.
"""

from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional

import httpx

from core import config
from models import metadata
from providers import openai_responses
from providers.base import OpenAICompatibleProvider, UpstreamError


# Curated capability overlay for OpenCode's free-tier models.
# Verified live (vision/tool-call probes + vendor docs).  This is NOT the
# source of truth for "is this model free" -- the live /models endpoint plus
# the ``-free`` suffix is.  The overlay only enriches ``desc``/vision for
# routing quality; any new ``*-free`` model not listed here is still served,
# with a synthetic desc and metadata-derived capabilities.  Entries without a
# ``-free`` suffix (``big-pickle``) need an explicit allowlist entry because
# suffix alone can't identify them; operator can also add more via
# ``LINGLING_EXTRA_FREE_MODELS`` without code change.
#
# ``api: "responses"`` marks Responses-only models (e.g. muse-spark) that Zen
# serves on POST /v1/responses, not /chat/completions.  The set is also
# extendable via ``LINGLING_RESPONSES_MODELS`` env var.
#
# Reasoning effort is deliberately absent here -- it comes from
# models.dev ``reasoning_options`` (see routing/effort.py) which is live per
# model and cannot be probed.
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
    "big-pickle": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "desc": "strong deliberate reasoning model for multi-step problem solving and tool use; text-only",
    },
    "muse-spark-1.2-contributor-free": {
        "vision": False, "reasoning": True, "coding": True, "tool_calls": True,
        "api": "responses",
        "desc": "coding-focused reasoning model; strong code generation, complex debugging and codebase understanding; text-only",
    },
}

# Extra free ids without ``-free`` suffix, supplied at runtime (comma-separated).
# Lets operators add a new suffix-less free model without touching code.
_EXTRA_FREE_IDS: set[str] = {
    s.strip() for s in __import__("os").getenv("LINGLING_EXTRA_FREE_MODELS", "").split(",") if s.strip()
}
# Extra Responses-only ids, same mechanism.
_EXTRA_RESPONSES_IDS: set[str] = {
    s.strip() for s in __import__("os").getenv("LINGLING_RESPONSES_MODELS", "").split(",") if s.strip()
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
        """Keep the fast chat route responsive when local egress is unhealthy.

        Fast-path models bypass the egress proxy pool entirely. Membership
        comes from the operator, not from a hardcoded model roster:

        - ``LINGLING_FAST_MODELS_DIRECT`` (comma-separated ids) names the
          casual-chat models that must stay responsive.
        - A pinned ``LINGLING_DISPATCHER_MODEL`` is implicitly exempt: the
          routing brain must answer fast so lingling-auto decisions never
          queue behind dead proxies. With no pin (the shipped default) no
          id is implicitly exempt -- the catalog-picked brain rotates egress
          like any other model.

        Every id can be forced back through the pool by setting
        ``LINGLING_FAST_MODELS_DIRECT=0``.
        """
        if not config.FAST_MODELS_DIRECT:
            return False
        if model_id in config.FAST_MODELS_DIRECT_IDS:
            return True
        if config.DISPATCHER_MODEL:
            return model_id == config.DISPATCHER_MODEL
        return False

    def _models_secret(self) -> Optional[str]:
        return None               # /models is keyless on OpenCode

    def is_model_free(self, model_id: str, meta: Dict[str, Any]) -> bool:
        # Primary signal: the ``-free`` suffix.  This is fully dynamic -- any
        # new ``*-free`` model OpenCode advertises tomorrow is served without a
        # code change.
        if model_id.lower().endswith("-free"):
            return True
        # Suffix-less free models (e.g. ``big-pickle``) need an explicit
        # allowlist.  The curated overlay covers known ones; the env var lets
        # operators add new ones without touching code.
        if model_id in FREE_MODEL_CAPS or model_id in _EXTRA_FREE_IDS:
            return True
        return False

    def build_model(self, model_id: str):
        """Enrich a model id, combining curated overlay with live models.dev data.

        * When a curated entry exists, its ``desc``/vision wins (hand-verified).
        * Otherwise capabilities are synthesized from live metadata + id tokens,
          so a brand-new ``*-free`` model is usable with no code change.
        * Context/effort always come from live metadata.
        """
        from providers.base import ProviderModel, _prettify
        caps = FREE_MODEL_CAPS.get(model_id)
        live = metadata.enrich(model_id, self.id)
        if caps:
            merged = dict(caps)
            merged["effort"] = live.get("effort") or []
            merged["reasoning_toggle"] = bool(live.get("reasoning_toggle"))
            return ProviderModel(
                id=model_id,
                provider_id=self.id,
                name=_prettify(model_id),
                free=True,
                vision=bool(caps.get("vision")),
                reasoning=bool(caps.get("reasoning")),
                context_length=live.get("context_length"),
                max_output=live.get("max_output"),
                modalities=["text", "image"] if caps.get("vision") else ["text"],
                capabilities=merged,
            )
        # No curated entry: synthesize desc from id signals + live reasoning flag,
        # so the dispatcher's capability table still has something to route on.
        # This is the fully-dynamic path for future models.
        base = super().build_model(model_id)
        # If the live enrich already produced a desc-free capabilities dict,
        # attach a synthetic one so dispatcher fallback scoring has signal.
        if not base.capabilities.get("desc"):
            mid = model_id.lower()
            hints: list[str] = []
            if base.vision:
                hints.append("multimodal: understands images")
            if "code" in mid:
                hints.append("code generation")
            if base.reasoning:
                hints.append("reasoning")
            if any(t in mid for t in ("flash", "mini", "lite")):
                hints.append("fast")
            if any(t in mid for t in ("ultra", "pro", "max")):
                hints.append("high capability")
            if hints:
                base.capabilities["desc"] = "; ".join(hints)
            else:
                base.capabilities["desc"] = "general-purpose free model"
        return base

    # -- Responses-API dispatching --------------------------------------
    # Zen hosts a subset of its free models on the OpenAI Responses API at
    # POST /v1/responses and exposes NOTHING for them on /chat/completions.
    # To a chat-completions client they look like a 500-then-failover when in
    # reality the model is healthy and just lives at a different URL. The
    # ``api: "responses"`` capability flag (set in ``FREE_MODEL_CAPS``) marks
    # those identifiers; the overrides below both translate chat shape <->
    # responses shape so callers (the executor) keep operating on chat JSON
    # regardless of which side of the upstream boundary a model lives on.

    def is_responses_model(self, model_id: str) -> bool:
        """Whether the listed model id is served only on the Responses API."""
        if model_id in _EXTRA_RESPONSES_IDS:
            return True
        caps = FREE_MODEL_CAPS.get(model_id) or {}
        return caps.get("api") == "responses"

    def _responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"

    def _responses_client_kwargs(
        self, proxy_url: Optional[str], timeout: Optional[float],
    ) -> Dict[str, Any]:
        """httpx.Client kwargs for a Responses upstream call.

        Mirrors the chat path's connect handling (``trust_env=False`` so the
        process's HTTP(S)_PROXY never silently uses an egress proxy; explicit
        ``proxy_url`` remains the proxy-pool's). Egress pool + first-token
        timeout semantics are identical to chat, so the executor's existing
        failure modes (cool a bad proxy, retry across the pool) keep working
        for the Responses branch unchanged.
        """
        kw: Dict[str, Any] = {
            "timeout": httpx.Timeout(timeout or config.REQUEST_TIMEOUT),
            "trust_env": False,
        }
        if proxy_url:
            kw["proxy"] = proxy_url
            kw["timeout"] = httpx.Timeout(
                timeout or config.REQUEST_TIMEOUT,
                connect=min(config.PROXY_CONNECT_TIMEOUT, float(timeout or config.REQUEST_TIMEOUT)),
            )
        return kw

    def chat_completions(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        """POST the upstream chat endpoint -- or, for Responses-only models,
        the Responses endpoint with the shapes translated by
        ``providers.openai_responses``. The runner up here is the executor:
        nothing in its discriminator changes whether the body that crossed
        the wire was Chat or Responses, because by the time it sees a result
        it is always a chat-completion dict.
        """
        if self.is_responses_model(model):
            return self._responses_nonstream(
                messages, model, secret, timeout=timeout, proxy_url=proxy_url, **params,
            )
        return super().chat_completions(
            messages, model, secret, timeout=timeout, proxy_url=proxy_url, **params,
        )

    def stream_chat(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        timeout: Optional[int] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Generator[bytes, None, None]:
        """Open a chat-completion stream -- or, for Responses-only models, a
        Responses stream that is reshaped to chat-completion SSE on the way
        out. The generator's surface is identical either way: OpenAI SSE
        chunks terminated with ``data: [DONE]\\n\\n``.
        """
        if self.is_responses_model(model):
            return self._responses_stream(
                messages, model, secret, timeout=timeout, proxy_url=proxy_url, **params,
            )
        return super().stream_chat(
            messages, model, secret, timeout=timeout, proxy_url=proxy_url, **params,
        )

    def _responses_nonstream(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        *, timeout: Optional[float] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        body = openai_responses.build_responses_body(
            model, messages, stream=False, **params,
        )
        # Use the cached client (set up in OpenAICompatibleProvider.__init__):
        # one TCP+TLS handshake across an arbitrary number of chat + Responses
        # requests through the same SOCKS5 tunnel. trust_env=False + the proxy
        # override are already wired by _client_for; nothing Responses-shape
        # needs to add beyond timeout choice.
        client = self._client_for(proxy_url, timeout)
        try:
            resp = client.post(
                self._responses_url(), json=body, headers=self.auth_headers(secret),
            )
        except httpx.HTTPError as exc:
            # Same UpstreamError normalization as the chat path so the executor
            # cools a dead proxy / fails over to the next egress on a 5xx-retry.
            self._evict_proxy_client(proxy_url)
            raise UpstreamError(504, str(exc), self.id) from exc
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text, self.id)
        try:
            payload = resp.json()
        except Exception as exc:  # malformed body -> 504-style availability failure
            raise UpstreamError(502, f"non-JSON Responses body: {exc}", self.id) from exc
        return openai_responses.response_to_chat_completion(payload, requested_model=model)

    def _responses_stream(
        self, messages: List[Dict[str, Any]], model: str, secret: str,
        *, timeout: Optional[float] = None, proxy_url: Optional[str] = None,
        **params: Any,
    ) -> Generator[bytes, None, None]:
        body = openai_responses.build_responses_body(
            model, messages, stream=True, **params,
        )
        client = self._client_for(proxy_url, timeout)
        try:
            with client.stream(
                "POST", self._responses_url(),
                json=body, headers=self.auth_headers(secret),
                timeout=self._stream_timeout(proxy_url, timeout),
            ) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", "replace")
                    raise UpstreamError(resp.status_code, detail, self.id)
                # Base camp-shape generator yields bytes; iter_lines yields
                # str. Encode for the translator before it inspects a prefix.
                upstream_lines = (
                    line.encode("utf-8") if isinstance(line, str) else line
                    for line in resp.iter_lines()
                    if line
                )
                yield from openai_responses.chat_sse_from_responses_sse(
                    upstream_lines, requested_model=model,
                )
        except httpx.TimeoutException as exc:
            # A timeout mid-stream is a stall, not a poisoned tunnel: keep the
            # cached client so the next request through this lane reuses the
            # warm TCP+TLS instead of re-handshaking. (See base.stream_chat.)
            raise UpstreamError(504, str(exc), self.id) from exc
        except httpx.HTTPError as exc:
            self._evict_proxy_client(proxy_url)
            raise UpstreamError(504, str(exc), self.id) from exc
