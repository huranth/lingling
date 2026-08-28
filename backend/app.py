"""Lingling gateway — a dumb routing proxy for free AI models.

Lingling takes whatever an OpenAI-compatible client (Cline, Claude Code, any
harness) sends and routes it to free models on OpenCode, defeating OpenCode's
per-IP free-tier limit via a rotating pool of egress proxies (Tor lanes)
identities by default). The client owns all prompting; Lingling only routes.

Endpoints
---------
GET    /api/health                     liveness + provider/catalog summary
GET    /api/models?refresh=0           multi-model entry + pooled free models
POST   /api/models/refresh             force a catalog re-fetch
GET    /api/providers                  per-provider status
GET    /api/providers/{pid}/keys       a provider's key pool (secret-free)
POST   /api/providers/{pid}/keys       add a key/token to a provider
DELETE /api/providers/{pid}/keys/{kid} remove a key from a provider
GET    /api/accounts                   OpenCode key pool (shortcut)
POST   /api/accounts                   add an OpenCode key (shortcut)
GET    /api/usage?limit=N              usage summary + recent requests
GET    /v1/models                       OpenAI-compatible model list
POST   /v1/chat/completions            OpenAI-compatible router
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core import config
from core import auth
from routing import dispatcher
from routing import effort
from routing import executor
from routing import parking
from routing import stream_guard
from routing import stream_idle
from routing import responses_bridge
from claudecode import messages_bridge
from claudecode import messages_response
from claudecode import messages_stream
from claudecode import model_map
from claudecode import thinking as cc_thinking
from models import vision_bridge
from models.catalog import UnifiedCatalog
from models.reconciler import OpenCodeReconciler
from providers import registry
from providers.base import extract_assistant_text, extract_usage, promote_reasoning_to_content
from providers.openai_responses import _MAX_OUTPUT_HEADROOM
from core.egress_helpers import _port_is_open
from usage.store import UsageStore
from tor.manager import TorManager
from tor import health as tor_health

VERSION = "0.2.0"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Lingling",
    version=VERSION,
    description="Dumb routing proxy for free AI models (OpenCode + IP rotation)",
)
# CORS is deliberately narrow. With allow_origins=["*"] any page on the
# internet could fire POST /api/tor/setup from a visitor's browser;
# credentialed requests need an explicit origin list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=auth.allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
log = logging.getLogger("uvicorn.error")
logging.getLogger("uvicorn.access").disabled = True
# Terminal cleanliness: one line per routed request, one per lifecycle event.
# Health-daemon and catalog chatter move to DEBUG unless LINGLING_VERBOSE=1.
if not config.VERBOSE:
    for _n in ("tor-health", "reconcile", "uvicorn.error"):
        _lg = logging.getLogger(_n)
        # Keep WARNING and above; INFO chatter (heal pokes, cycle verdicts)
        # is debug-only in quiet mode.
        if _lg.level == 0:
            _lg.setLevel(logging.WARNING)

def _startup_sync_tor(*, start_daemon: bool = True) -> None:
    """Sync already-running Tor lanes into the pool, then start the daemon.

    tor.exe lanes persist across Lingling
    restarts as standalone subprocesses, so running ``start_all()`` again
    would skip-or-duplicate them; instead we sync their URLs into the proxy
    pool directly so the cross-family rotation works immediately without the
    user clicking Start. The Tor health daemon then takes over verification
    + healing.

    When ``LINGLING_BOOTSTRAP_TOR=1`` the daemon start is deferred to
    :func:`_bootstrap_tor` so its first cycle doesn't race with the background
    lane-launch thread.
    """
    try:
        added = _sync_tor_to_pool()
        if added:
            log.info("startup: hoisted %d running Tor lanes back into the pool -- they were cooking the whole time", added)
        elif tor_manager.status()["lanes_running"] > 0:
            log.info("startup: Tor lanes already in the pool (%d cooking) -- nothing to do",
                     tor_manager.status()["lanes_running"])
        else:
            log.info("startup: no running Tor lanes left to re-sync")
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: Tor sync went sideways (%s) -- skipping", exc)

    if start_daemon:
        try:
            tor_health_daemon.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("startup: Tor health daemon failed to start (%s)", exc)


    # Trim the request log. Without this it grows for the life of the install.
    try:
        removed = usage_store.prune(config.USAGE_RETENTION_DAYS)
        if removed:
            log.info(
                "startup: pruned %d stale usage rows (older than %d days)",
                removed, config.USAGE_RETENTION_DAYS,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: usage prune went sideways (%s) -- skipping", exc)

def _bootstrap_tor() -> None:
    """Download tor.exe + launch the N Tor lanes in-process (LINGLING_BOOTSTRAP_TOR=1).

    Doing this here rather than over HTTP is deliberate:
    ``/api/tor/setup`` + ``/api/tor/start`` are authenticated, so ``start.bat``
    would otherwise need a credential to bootstrap the server it just launched.
    A missing stem/tor.exe or a failed download is reported and swallowed --
    the gateway keeps serving directly.
    """
    try:
        st = tor_manager.status()
        if st["lanes_running"] < tor_manager.count:
            log.info("bootstrap: cooking fresh Tor lanes (one-time, slow -- tor.exe download + circuit build)")
            tor_manager.setup_lanes(log=log.info)
        res = _start_tor_at_startup()
        log.info(
            "bootstrap: Tor's ready (started=%d, failed=%d, skipped=%d)",
            res.get("started", 0), res.get("failed", 0), res.get("skipped", 0),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bootstrap: Tor setup went sideways (%s) -- continuing without it", exc)
    finally:
        # Start the health daemon *after* bootstrap so its first cycle doesn't
        # race with lane launch and pool sync.
        try:
            tor_health_daemon.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("bootstrap: Tor health daemon start went sideways (%s)", exc)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown for the gateway.

    Replaces the deprecated @app.on_event hooks and adds the shutdown half the
    health daemon never had. The single egress family comes up here:

    * Tor lanes (tor-bootstrap thread) -- slow on first run (downloads tor.exe
      + builds circuits) so it runs off the event loop.

    A missing stem/tor.exe or a failed download leaves the gateway serving
    directly rather than blocking startup.
    """
    # Sync any already-running Tor lanes into the pool (cross-restart reuse),
    # and let _bootstrap_tor download+launch when the env flag is set. The
    # bootstrap thread is daemon, so it never blocks exit.
    _startup_sync_tor(start_daemon=not config.BOOTSTRAP_TOR)
    if config.BOOTSTRAP_TOR:
        threading.Thread(target=_bootstrap_tor, name="tor-bootstrap", daemon=True).start()

    # Burn-state reconcile against OpenCode Zen. The catalog's in-memory state
    # is empty at boot, so first refresh + apply_persisted_state together are
    # what the operator's first request sees. The synchronous run_once below is
    # the *only* tick that has to block startup -- every later tick is daemon.
    try:
        catalog.refresh(force=True)
        catalog.apply_persisted_state()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: burn-state rehydrate refused to come up (%s)", exc)
    try:
        reconciler.run_once()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: first reconcile tick blew up (%s) -- continuing anyway", exc)
    try:
        reconciler.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: reconciler daemon refused to come up (%s)", exc)

    try:
        yield
    finally:
        try:
            reconciler.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: reconciler stop went sideways (%s)", exc)
        try:
            tor_health_daemon.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: tor health daemon stop went sideways (%s)", exc)
        try:
            # Stop the Tor lanes on the way out (loopback ports + control
            # sockets). The kill job already arms against orphans; this is the
            # polite path and the POSIX-only path.
            tor_manager.stop_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: tor stop went sideways (%s)", exc)
        try:
            usage_store.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: usage store close went sideways (%s)", exc)


app.router.lifespan_context = lifespan


# Request fields Lingling handles itself. Everything else is forwarded to
# the upstream provider unchanged. OpenCode already manages streaming,
# reasoning, and BPE decoding; we do not rewrite its output.
#
# The executor's own parameter names are excluded too. `**params` is splatted
# into `run_in_threadpool(executor.execute_*, ...)`, so a body carrying any of
# these collided with the positional or keyword argument of the same name and
# raised TypeError -> HTTP 500. `timeout` is the realistic one: a plausible
# field for a client to send, and it killed the streaming path while the
# non-streaming path survived (different call shape).
_PASSTHROUGH_EXCLUDE = frozenset({
    "model", "messages", "stream", "lingling", "session_id", "lingling_recover",
    # executor.execute_nonstream / execute_stream signature
    "model_id", "providers", "proxy_pool", "timeout",
    # reasoning-blank recovery: internal knobs threaded into the executor, never
    # client-controlled values. Without these a body carrying {"pin_proxy_id": ...}
    # or {"proxy_ref": ...} could collide with the kwarg and raise TypeError.
    "pin_proxy_id", "proxy_ref",
    # recycler: catalog handle threaded into the executor for success-heal /
    # burn-mark; never a client-controlled value. Without this allowlist entry a
    # body carrying {"catalog": ...} could collide with the kwarg.
    "catalog",
    # starlette.concurrency.run_in_threadpool signature
    "func",
})
def _passthrough_params(body: Dict[str, Any]) -> Dict[str, Any]:
    """Forward every OpenAI-compatible request parameter except fields Lingling manages."""
    return {k: v for k, v in body.items() if k not in _PASSTHROUGH_EXCLUDE}


def _header_safe(value: str) -> str:
    """Make a string safe to put in an HTTP header value.

    Starlette encodes header values as latin-1, so any character above U+00FF
    raises UnicodeEncodeError when the response is constructed. The dispatcher's
    ``reason`` is model-generated free text and routinely contains an em-dash or
    smart quotes, so this is a normal path, not an adversarial one.

    Only the X-Lingling-* mirror is sanitised; the full reason still reaches the
    client intact in the JSON/SSE body.
    """
    if not value:
        return ""
    for uni, ascii_ in (
        ("\u2014", "--"), ("\u2013", "-"), ("\u2018", "'"), ("\u2019", "'"),
        ("\u201c", '"'), ("\u201d", '"'), ("\u2026", "..."), ("\u2022", "*"),
    ):
        value = value.replace(uni, ascii_)
    # Control characters must go before the latin-1 pass: CR, LF and NUL are all
    # valid latin-1, so they survive it, and Starlette then refuses the header
    # with RuntimeError("Invalid HTTP header value") -- aborting the response and
    # leaving the client with nothing. The reason is model-generated text, so a
    # stray line break in it is a normal occurrence, not an attack.
    value = "".join(" " if ch in "\r\n\t" else ch for ch in value if ch >= " ")
    return value.encode("latin-1", "replace").decode("latin-1")


def _harvest_stream_usage(raw: bytes, into: Dict[str, Any]) -> None:
    """Read token counts out of an SSE line without altering the byte stream.

    OpenCode's final content chunk carries an OpenAI-shaped ``usage`` object,
    and it also emits a proprietary ``x-opencode-type: inference-cost`` frame
    with a ``normalizedUsage`` block. Both are accepted; later frames win so
    the most complete numbers survive. Anything unparseable is ignored --
    telemetry must never be able to break a response.

    Three keys with an underscore prefix are kept alongside the token counts
    so the chat-stream blank-turn recovery (sibling #3 of the
    nemotron-blank-turn family -- mirror of ``providers.base.promote_reasoning_to_content``
    which handles the non-stream chat and the Responses-bridge siblings) can
    decide whether a stream ended blank with reasoning text to surface:

    * ``_visible_content_chars`` -- running total of visible text in every
      ``content`` delta (string or structured parts). Zero at end-of-stream
      means no content frame reached the client.
    * ``_reasoning_text`` -- concatenated reasoning text gathered from
      ``reasoning_content`` / ``reasoning`` / ``thinking`` delta keys. Used as
      the fallback promotion payload when ``_visible_content_chars`` is zero.
    * ``_finish_reason`` -- the last ``finish_reason`` seen; ``length``
      paired with zero visible content is the canonical blank-turn signature.
    * ``_resume_items`` -- completed upstream reasoning items (with their
      ``encrypted_content``) that the Responses-only stream reshaper passes
      through under a namespaced delta key. A turn that ends with nothing
      but thinking is the model's own think-phase boundary; the blank-turn
      recovery resumes it by sending these items back as input, which
      continues the model's thinking instead of starting over.
    """
    if not raw.startswith(b"data:"):
        return
    payload = raw[5:].strip()
    if not payload or payload == b"[DONE]":
        return
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(obj, dict):
        return

    usage = obj.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("prompt_tokens"), int):
            into["tokens_in"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            into["tokens_out"] = usage["completion_tokens"]
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            into["reasoning_tokens"] = details["reasoning_tokens"]

    norm = obj.get("normalizedUsage")
    if isinstance(norm, dict):
        for src_key, dst_key in (("inputTokens", "tokens_in"),
                                 ("outputTokens", "tokens_out"),
                                 ("reasoningTokens", "reasoning_tokens")):
            value = norm.get(src_key)
            if isinstance(value, int):
                into[dst_key] = value

    # Sibling-#3 tracking: only accumulate when choices carry something we
    # can later use to surface a blank turn. Skip otherwise to keep the path
    # hot for the common case (the to-message-then-usage frames nemotron /
    # muse emit, and the usage-only final chunk that OpenAI stream_options
    # adds, which has an empty choices list on purpose).
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            delta = ch.get("message")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content.strip():
                into["_visible_content_chars"] = (
                    into.get("_visible_content_chars", 0) + len(content)
                )
            elif isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict)
                            and isinstance(part.get("text"), str)
                            and part["text"]):
                        into["_visible_content_chars"] = (
                            into.get("_visible_content_chars", 0) + len(part["text"])
                        )
            # Mirror the first-key-wins order sibling #5 uses for non-stream:
            # ``reasoning_content`` (deepseek/ling), then ``reasoning``
            # (nemotron), then ``thinking`` (Anthropic). Break after the first
            # present key so a model that ships both reasoning and a derived
            # sibling at the same delta isn't counted twice.
            for reason_key in ("reasoning_content", "reasoning", "thinking"):
                rv = delta.get(reason_key)
                if isinstance(rv, str) and rv:
                    into["_reasoning_text"] = into.get("_reasoning_text", "") + rv
                    break
        fr = ch.get("finish_reason")
        if fr:
            into["_finish_reason"] = fr
        # A tool-call turn is never blank: Codex renders the call even when no
        # text delta ever arrived, so the blank-turn retry must not discard it.
        if isinstance(delta, dict) and delta.get("tool_calls"):
            into["_tool_calls"] = into.get("_tool_calls", 0) + 1
        # Muse thinking-phase passthrough: the Responses-only stream reshaper
        # emits completed upstream reasoning items (with their
        # ``encrypted_content``) under this namespaced delta key. Accumulate
        # them so the blank-turn recovery can resume the model's own thinking
        # instead of re-running the request from scratch.
        if isinstance(delta, dict) and delta.get("lingling_resume_items"):
            items = delta["lingling_resume_items"]
            if isinstance(items, list):
                into["_resume_items"] = into.get("_resume_items", []) + [
                    item for item in items if isinstance(item, dict)
                ]


def _chat_blank_recovery_frame(outcome, seen, target) -> Optional[bytes]:
    """Build the SSE recovery frame for a chat stream that landed blank.

    Streaming sibling #3 of the nemotron-blank-turn family, mirroring
    ``providers.base.promote_reasoning_to_content`` for non-stream chat. A
    reasoning-only turn that emits no visible ``content`` delta would reach an
    OpenAI chat client as an empty reply; the non-stream chat path already
    promotes the reasoning text inline, and the Responses bridge does its
    equivalent via `response.output_text.delta` -- this injects the same
    promotion into the streaming chat bytes right before ``[DONE]``.

    Two recoveries, both one synthetic ``content`` delta framed as a normal
    chunk so a chat client concatenates them like any other:

    * If reasoning text was visible during the stream, mirror it verbatim
      (same outcome as the non-stream sibling #5: the reasoning WAS the
      answer).
    * If reasoning_tokens were spent but no reasoning text was surfaced (a
      reasoning model whose upstream hides thinking), say so plainly -- a
      blank turn with thinking-token billing but zero text anywhere is the
      most disorienting one for a client. We never claim the missing bytes
      were real model output.

    Returns ``None`` when nothing should be injected: the visible content
    count is non-zero (the client got a real reply), the stream errored or
    was mid-flight-recovered onto a fresher stream (either duplicates the
    delta or contradicts the recovery marker), or we lack any evidence of a
    blank-but-reasoning turn at all. A rescue attempt that is in flight or
    completed always wins (the rescue pushes a reset frame first; injecting
    a synthetic after it contradicts the discard instruction). The error
    path -- a stream that broke mid-flight without ever reaching recovery --
    still surfaces reasoning text so a Muse-style "thought then died" turn
    becomes visible verbatim.
    """
    # Successful rescue wins: the rescue pushed a reset frame, the new stream's
    # content IS the answer, and a synthetic after it contradicts the discard.
    # A rescue-attempted-and-also-failed turn falls through -- its reasoning
    # is the only answer we have left to surface (muse-spark's no-finish case).
    # A bare-broken stream (no rescue enabled) is left alone: there's no signal
    # to distinguish "the model said nothing" from "we lost the wire", so we
    # never silently author an answer there.
    if outcome.recovered and not outcome.error:
        return None
    if not outcome.recovered and outcome.error:
        return None
    # Already produced visible content: don't double up. A tool-call turn is
    # never blank either -- the client is waiting on those arguments.
    if seen.get("_visible_content_chars", 0) > 0 or seen.get("_tool_calls", 0) > 0:
        return None
    # No signal at all -- no reasoning text, no reasoning tokens billed.
    # A true blank is left alone rather than rewritten as a phantom message.
    # Per latest user request the empty must never reach the CLI — the stream
    # will be retried silently at the app layer instead of surfacing a notice.
    if not seen.get("_reasoning_text", "").strip() and not seen.get("reasoning_tokens", 0):
        return None
    reasoning_text = seen.get("_reasoning_text", "") or ""
    reasoning_tokens = seen.get("reasoning_tokens", 0) or 0
    finish_reason = seen.get("_finish_reason")
    if reasoning_text.strip():
        content = reasoning_text
    elif reasoning_tokens > 0:
        # Sibling #6: zero visible with billed reasoning but no text.
        content = (
            "[lingling: stream produced no visible content; upstream "
            f"billed {reasoning_tokens} reasoning_tokens "
            + (f"then finish_reason={finish_reason}. " if finish_reason else "")
            + "Increase max_tokens or retry.]"
        )
    else:
        return None
    payload = {"choices": [{
        "index": 0,
        "delta": {"content": content},
        "finish_reason": None,
    }]}
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def stream_with_blank_chat_recovery(
    gen: Generator[bytes, None, None],
    outcome,
    seen: Dict[str, Any],
    requested: str,
    target: str,
    log,
) -> Generator[bytes, None, None]:
    """Forward chat SSE bytes verbatim, injecting a recovery delta before [DONE].

    Wraps :func:`routing.stream_guard.guarded_stream` for the chat path: a
    stream that finishes blank (zero visible content) and never produced a
    reasoning-text sibling field is otherwise indistinguishable from
    "the model said nothing" -- the chat sibling of the nemotron-blank-turn
    family that the non-stream path already handles via
    ``promote_reasoning_to_content``. Here we don't have a single assembled
    ``message`` dict to mutate, so the recovery is one synthetic ``content``
    delta just upstream of the ``[DONE]`` sentinel, which keeps the order of
    frames a chat client parses unchanged.

    The wrapper never re-finishes the turn: ``finish_reason`` was already
    announced by the real upstream and is left untouched. The recovery is
    opt-in (no caller wires it for the Responses or Messages bridge, which
    have their own dedicated siblings).
    """
    injected = False
    for raw in gen:
        if (not injected and b"data: [DONE]" in raw):
            synth = _chat_blank_recovery_frame(outcome, seen, target)
            if synth is not None:
                log.info(
                    "chat stream blank-turn recovery requested=%s target=%s: "
                    "no visible content; injecting synthetic final delta",
                    requested, target,
                )
                yield synth
            injected = True
        yield raw
    # Some upstreams close the connection without an explicit ``[DONE]``
    # marker -- guard the same recovery against that case so we don't drop
    # it silently when Zen skips [DONE] under load.
    if not injected:
        synth = _chat_blank_recovery_frame(outcome, seen, target)
        if synth is not None:
            log.info(
                "chat stream blank-turn recovery requested=%s target=%s: "
                "no visible content, no [DONE] marker; appending final delta",
                requested, target,
            )
            yield synth



# Core singletons (module-level so tests can introspect/patch them).
providers = registry.build_providers()
proxy_pool = registry.build_proxy_pool()
catalog = UnifiedCatalog(providers)
# Start-up + periodic recycler-vs-upstream reconcile. The first run happens
# synchronously inside lifespan() so the user's first request sees a verified
# catalog; the daemon thread on the same instance handles subsequent ticks.
reconciler = OpenCodeReconciler(catalog, providers["opencode"], proxy_pool=proxy_pool)
usage_store = UsageStore()

# Core egress singletons: the Tor lane pool + its auto-healing health daemon.
# The manager owns N separate tor.exe pinned to distinct exit countries,
# registered into the shared proxy_pool as first-class members so least-loaded-
# first rotates across the distinct lanes. Lazy stem import inside the manager
# means a missing stem/tor.exe never blocks a direct run; the lane status
# surfaces "Tor unavailable" instead of crashing the gateway.
tor_manager = TorManager(
    root_dir=Path(config.DATA_DIR) / "tor",
    count=getattr(config, "TOR_LANE_COUNT", 5),
    exit_countries=getattr(config, "TOR_EXIT_COUNTRIES", None),
    socks_base=getattr(config, "TOR_SOCKS_BASE_PORT", 52001),
    control_base=getattr(config, "TOR_CONTROL_BASE_PORT", 52301),
    tor_exe=getattr(config, "TOR_EXE", ""),
    boot_timeout=getattr(config, "TOR_BOOT_TIMEOUT", 120),
    log=log.info,
)
# Auto-healing Tor health daemon: periodically verifies every lane (SOCKS port
# liveness + an OpenCode reachability probe + an IsTor probe that captures the
# real distinct exit IP per lane for the dashboard) and regenerates dead or
# burned lanes automatically so outages self-heal.
tor_health_daemon = tor_health.TorHealthDaemon(
    tor_manager, proxy_pool,
    check_interval=getattr(config, "TOR_HEALTH_INTERVAL", 60),
    min_healthy=getattr(config, "TOR_MIN_HEALTHY", 3),
    log=log.info,
)

def _sync_tor_to_pool() -> int:
    """Register every running Tor lane SOCKS5 proxy into the ProxyPool.

    A simple port check only -- the Tor
    health daemon (``tor_health_daemon``) does the deeper IsTor + OpenCode
    probes and removes unhealthy lanes within its next cycle. This populates
    the pool immediately at startup (tor.exe lanes persist across Lingling
    restarts as standalone subprocesses) so traffic rotates across the lanes
    without a manual click.
    """
    existing = {p.id: p for p in proxy_pool.get_all_proxies()}
    added = 0
    for lane in tor_manager.lanes:
        if not lane._is_running():
            continue
        pid = f"tor-{lane.index}"
        if pid in existing:
            # Port can change under a regenerate; write through the pool so
            # the executor reads the new url under the pool's lock.
            proxy_pool.set_url(pid, lane.proxy_url)
        else:
            proxy_pool.add(
                lane.proxy_url,
                proxy_id=pid,
                label=f"Tor lane #{lane.index} {{{lane.exit_country}}}",
            )
            added += 1
    return added




def _start_tor_at_startup() -> Dict[str, Any]:
    """Auto-start existing Tor lanes and sync them to the proxy pool.

    Launches any lanes not already running and feeds them into ``proxy_pool``. A missing stem or tor.exe is reported as a soft "could not
    start" (never raises) so the tor-bootstrap thread never tears down the
    gateway continues to serve directly.
    """
    if not tor_manager.stem_available():
        return {"started": False, "reason": "stem not installed",
                "stem_available": False, "tools_ready": tor_manager.tools_ready(),
                "pool": proxy_pool.status()}
    if not tor_manager.tools_ready():
        # First run: tools haven't been downloaded yet. start_all refuses
        # without a tor binary, so surface that rather than let it raise.
        return {"started": False, "reason": "tor not set up (POST /api/tor/setup)",
                "stem_available": True, "tools_ready": False,
                "pool": proxy_pool.status()}
    try:
        res = tor_manager.start_all(log=log.info)
    except Exception as exc:  # noqa: BLE001
        log.warning("tor auto-start failed: %s", exc)
        return {"started": False, "reason": f"start_all failed: {exc}",
                "pool": proxy_pool.status()}
    added = _sync_tor_to_pool()
    res["pool"] = proxy_pool.status()
    res["synced_to_pool"] = added
    # Boolean-vs-count fix:: the boolean "did
    # anything start?" flag lives in ``any_started`` so the integer ``started``
    # count (e.g. 5 lanes launched) survives intact. The original line --
    # ``res["started"] = res.get("started", 0) > 0`` -- overwrote the integer
    # with a boolean (``5 > 0`` -> ``True``), and ``_bootstrap_tor``'s log
    # line ``"started=%d" % res.get("started", 0)`` formatted ``True`` as
    # ``1``: a 5-lane bootstrap surfaced "started=1" in the gateway logs,
    # which is exactly the miscount surfaced in the gateway logs.
    res["any_started"] = res.get("started", 0) > 0
    return res

# ---------------------------------------------------------------------------
# Health, models, providers, key router, usage
# ---------------------------------------------------------------------------
@app.api_route("/api/hello", methods=["GET", "HEAD"])
def hello() -> Dict[str, Any]:
    """Liveness probe Claude Code sends on startup.

    Not a documented Anthropic route, but Claude Code HEADs it to decide whether
    the endpoint is reachable. Answering it keeps the backend log honest: the
    alternative was a 401 `auth reject` line on every session start, which reads
    like a problem and is not one.
    """
    return {"ok": True, "name": "Lingling"}


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """Liveness plus a secret-free summary of providers, catalog and pool."""
    return {
        "status": "ok",
        "name": "Lingling",
        "version": VERSION,
        "usage_retention_days": config.USAGE_RETENTION_DAYS,
        "providers": {pid: prov.status() for pid, prov in providers.items()},
        "catalog": catalog.meta(),
        "proxies": proxy_pool.status(),
    }


def _multimodel_entry() -> Dict[str, Any]:
    """The single hard-coded model: the multi-model router itself."""
    # The dispatcher field mirrors what routing actually uses as its brain:
    # the pinned DISPATCHER_MODEL when one is configured, otherwise the model
    # auto-promoted from the live catalog (which changes as models burn and
    # un-burn). Reporting the raw config would show "" on a default install.
    brain = dispatcher.dispatcher_model(catalog)
    return {
        "id": config.MULTIMODEL_ID,
        "name": config.MULTIMODEL_NAME,
        "description": config.MULTIMODEL_DESCRIPTION,
        "free": True,
        "vision": True,
        "reasoning": True,
        "multi_model": True,
        "dispatcher": brain,
        "providers": [p.display_name for p in providers.values()],
        "context_length": None,
        "max_output": None,
        "modalities": ["text", "image"],
        "provider": "lingling",
    }


def _openai_model_entry(model: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Lingling catalog model into the OpenAI `/v1/models` shape."""
    mid = model.get("id") or ""
    return {
        "id": mid,
        "object": "model",
        "created": 0,
        "owned_by": model.get("provider") or "lingling",
        "root": mid,
        "parent": None,
        "permission": [],
        "name": model.get("name") or mid,
        "description": model.get("description") or "",
        "context_length": model.get("context_length"),
        "max_output": model.get("max_output"),
        "modalities": model.get("modalities") or (["text", "image"] if model.get("vision") else ["text"]),
    }


@app.get("/api/models")
def get_models(refresh: int = 0) -> Dict[str, Any]:
    """The multi-model entry plus the pooled FREE models from all providers.

    Premium models are filtered out; each model lists the providers that serve it.
    """
    catalog.refresh(force=bool(refresh))
    return {
        "multimodel": _multimodel_entry(),
        "models": [lm.to_dict() for lm in catalog.free()],
        "meta": catalog.meta(),
    }


@app.get("/v1")
def v1_info() -> Dict[str, Any]:
    """Small compatibility landing page for clients pointed at `/v1`.

    Codex/Cline and other OpenAI-compatible clients normally call `/v1/models` and
    `/v1/chat/completions`; this makes a browser/manual probe of the base URL
    less confusing.
    """
    return {
        "object": "api.info",
        "name": "Lingling OpenAI-compatible API",
        "version": VERSION,
        "endpoints": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
        },
    }


@app.get("/v1/models")
def v1_models(refresh: int = 0) -> Dict[str, Any]:
    """OpenAI-compatible model list for Codex/Cline model pickers.

    Response shape intentionally matches OpenAI:

        {"object":"list", "data":[{"id":"...", "object":"model", ...}]}
    """
    catalog.refresh(force=bool(refresh))
    models = [_openai_model_entry(_multimodel_entry())]
    models.extend(_openai_model_entry(m.to_dict()) for m in catalog.free())
    return {"object": "list", "data": models}


@app.post("/api/models/refresh")
def refresh_models() -> Dict[str, Any]:
    catalog.refresh(force=True)
    return {
        "multimodel": _multimodel_entry(),
        "models": [lm.to_dict() for lm in catalog.free()],
        "meta": catalog.meta(),
    }


@app.get("/api/providers")
def get_providers() -> Dict[str, Any]:
    return {"providers": [prov.status() for prov in providers.values()]}


def _get_provider(pid: str):
    prov = providers.get(pid)
    if prov is None:
        raise HTTPException(404, f"unknown provider '{pid}'")
    return prov


@app.get("/api/providers/{pid}/keys")
def get_keys(pid: str) -> Dict[str, Any]:
    return _get_provider(pid).keys.status()


class KeyIn(BaseModel):
    """A credential to add to a provider's key pool.

    Mirrors the dict shape ``providers.key_pool.KeyPool.from_list`` accepts
    (``secret``/``api_key``/``key``/``token``, optional ``label`` and ``id``)
    so a caller can post any of those names; :meth:`resolved_secret` picks the
    first one that is present and non-empty.
    """
    secret: Optional[str] = None
    api_key: Optional[str] = None
    key: Optional[str] = None
    token: Optional[str] = None
    label: str = ""
    id: str = ""

    def resolved_secret(self) -> str:
        return self.secret or self.api_key or self.key or self.token or ""


@app.post("/api/providers/{pid}/keys")
def add_key(pid: str, body: KeyIn) -> Dict[str, Any]:
    prov = _get_provider(pid)
    secret = body.resolved_secret().strip()
    if not secret:
        raise HTTPException(400, "a credential (secret/api_key/key/token) is required")
    key = prov.keys.add(secret, body.label, body.id)
    return {"added": key.status(), "pool": prov.keys.status()}


@app.delete("/api/providers/{pid}/keys/{kid}")
def remove_key(pid: str, kid: str) -> Dict[str, Any]:
    prov = _get_provider(pid)
    removed = prov.keys.remove(kid)
    if not removed:
        raise HTTPException(404, f"key '{kid}' not found")
    return {"removed": kid, "pool": prov.keys.status()}


# OpenCode shortcuts (the OpenCode key router), kept for convenience.
@app.get("/api/accounts")
def get_accounts() -> Dict[str, Any]:
    return _get_provider("opencode").keys.status()


@app.post("/api/accounts")
def add_account(body: KeyIn) -> Dict[str, Any]:
    return add_key("opencode", body)


# ---------------------------------------------------------------------------
# Egress proxy pool (defeats OpenCode's IP-based free-tier limits)
# ---------------------------------------------------------------------------
class ProxyIn(BaseModel):
    """An egress proxy URL to add to the rotation pool.

    The URL may be supplied under any of these keys (``url`` preferred).
    Supported schemes: http://, https://, socks5://, socks4://.
    """
    url: Optional[str] = None
    proxy: Optional[str] = None
    server: Optional[str] = None
    label: str = ""
    id: str = ""

    def resolved_url(self) -> str:
        return self.url or self.proxy or self.server or ""


@app.get("/api/proxies")
def get_proxies_list() -> Dict[str, Any]:
    """Secret-free status of every proxy in the pool (credentials redacted)."""
    return proxy_pool.status()


@app.post("/api/proxies")
def add_proxy(body: ProxyIn) -> Dict[str, Any]:
    url = body.resolved_url().strip()
    if not url:
        raise HTTPException(400, "a proxy url (url/proxy/server) is required")
    if not (url.startswith("http://") or url.startswith("https://")
            or url.startswith("socks5://") or url.startswith("socks4://")):
        raise HTTPException(400, "url must use http(s):// or socks5:// scheme")
    px = proxy_pool.add(url, body.label, body.id)
    return {"added": px.status(), "pool": proxy_pool.status()}


@app.delete("/api/proxies/{pid}")
def remove_proxy(pid: str) -> Dict[str, Any]:
    removed = proxy_pool.remove(pid)
    if not removed:
        raise HTTPException(404, f"proxy '{pid}' not found")
    return {"removed": pid, "pool": proxy_pool.status()}


# ---------------------------------------------------------------------------
# Tor lanes (genuine exit-IP diversity -- the single egress family)
# ---------------------------------------------------------------------------
class TorCountIn(BaseModel):
    """Optional body for the Tor control endpoints. ``lane`` (1-based) targets a
    single lane in renew/refresh; omit to apply to every running lane. ``count``
    is accepted for symmetry with the other control endpoints but, like the
    -- lane
    count is fixed at construction time -- pass a different value and you get a
    400 telling you to restart with LINGLING_TOR_COUNT instead."""

    count: Optional[int] = None
    lane: Optional[int] = None


# Tor lanes (the single egress family -- genuine exit-IP diversity)
# ---------------------------------------------------------------------------
@app.get("/api/tor")
def tor_status() -> Dict[str, Any]:
    """Tor manager status: lane count, how many running, configured exit
    countries, distinct exit IPs, and per-lane control-port aliveness."""
    return tor_manager.status()


@app.post("/api/tor/setup")
def tor_setup(body: TorCountIn = TorCountIn()) -> Dict[str, Any]:
    """One-time: download the Tor Expert Bundle + write per-lane torrc.

    Idempotent -- existing lane configs are kept. Does NOT launch tor; ``POST
    /api/tor/start`` does. The lane count is fixed at
    construction time, so ``count`` differing from the manager's count is a 400
    pointing the user at ``LINGLING_TOR_COUNT`` on restart.
    """
    if body.count is not None and body.count != tor_manager.count:
        raise HTTPException(
            400,
            f"this manager was built for {tor_manager.count} Tor lanes. "
            f"Restart Lingling with LINGLING_TOR_COUNT={body.count} to change it.",
        )
    try:
        return tor_manager.setup_lanes(log=log.info)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Tor setup failed: {exc}")


@app.post("/api/tor/start")
def tor_start() -> Dict[str, Any]:
    """Launch all configured Tor lanes and feed them into the proxy pool."""
    if not tor_manager.stem_available():
        raise HTTPException(409, "stem not installed (run `pip install stem==1.8.2`)")
    if not tor_manager.tools_ready() and not config.TOR_EXE:
        raise HTTPException(409, "Tor not set up yet. POST /api/tor/setup first.")
    return _start_tor_at_startup()


@app.post("/api/tor/stop")
def tor_stop() -> Dict[str, Any]:
    """Stop every Tor lane (proxies leave the pool, just offline)."""
    stop_result = tor_manager.stop_all()
    removed: List[str] = []
    for px in proxy_pool.get_all_proxies():
        if px.id.startswith("tor-"):
            proxy_pool.remove(px.id)
            removed.append(px.id)
    return {"stopped": stop_result.get("stopped", 0),
            "removed_from_pool": removed, "pool": proxy_pool.status()}


@app.get("/api/tor/health")
def tor_health(probe: bool = False) -> Dict[str, Any]:
    """Tor health: TCP-check + (optional) full SOCKS5+IsTor probe per lane.

    ``probe=true`` runs a fresh ``check_and_heal`` cycle now (which probes
    OpenCode reachability + check.torproject.org/api/ip through each live lane
    and fills in each lane's measured ``exit_ip``). Without ``probe``, this is
    a cheap snapshot derived from each lane's recorded state -- no network
    round-trip -- for dashboard polling.
    """
    if probe:
        try:
            return tor_health_daemon.check_and_heal()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Tor health probe failed: {exc}")

    results: List[Dict[str, Any]] = []
    alive = dead = 0
    for lane in tor_manager.lanes:
        pid = f"tor-{lane.index}"
        px = proxy_pool.get_by_id(pid)
        port_open = _port_is_open("127.0.0.1", lane.socks_port, timeout=0.5)
        control_alive = _port_is_open("127.0.0.1", lane.control_port, timeout=0.2)
        is_alive = port_open and control_alive
        if is_alive:
            alive += 1
        else:
            dead += 1
        results.append({
            "index": lane.index,
            "socks_port": lane.socks_port,
            "control_port": lane.control_port,
            "exit_country": lane.exit_country,
            "exit_ip": lane.exit_ip,
            "running": lane._is_running(),
            "control_alive": control_alive,
            "port_open": port_open,
            "opencode_reachable": None,
            "is_tor": None,
            "consecutive_failures": px.consecutive_failures if px else 0,
            "total_429": px.total_429 if px else 0,
            "in_pool": px is not None,
            "last_circuit_built_ts": lane.last_circuit_built_ts,
            "renewing": lane.renewing,
            "boot_ok": lane.boot_ok,
            # Surfaced alongside `renewing`/`boot_ok` so the dashboard can
            # render the `probing` (unverified) chip before the first probe has
            # actually run, and the `healing` chip while the daemon is
            # mid-restart/regenerate -- so the
            # two racks' health rows carry the same flags.
            "probing": lane.probing,
            "healing": lane.healing,
            "pid": pid,
            "healthy": is_alive,
        })
    return {
        "total": len(tor_manager.lanes),
        "alive": alive, "dead": dead,
        "probe": False,
        "tor_ready": tor_manager.status()["tor_ready"],
        "instances": results,
    }


@app.post("/api/tor/renew")
def tor_renew(body: TorCountIn = TorCountIn()) -> Dict[str, Any]:
    """NEWNYM quick-heal: ask every (or one specified) lane for fresh circuits.

    This is the *fast* sweep the dashboard's "Renew circuits" button fires --
    NEWNYM is ~10s-rate-limited and does not guarantee a *new* exit IP per the
    Tor spec, so callers wanting a guaranteed fresh exit should use
    ``/api/tor/refresh`` instead (rebuild_circuits + await_build).
    """
    lanes = _resolve_tor_lanes(body)
    results: List[Dict[str, Any]] = []
    for lane in lanes:
        if not lane._is_running():
            results.append({"index": lane.index, "ok": False, "reason": "lane not running"})
            continue
        results.append(tor_manager.renew_circuit(lane, log=log.info))
    return {"results": results, "pool": proxy_pool.status()}


@app.post("/api/tor/refresh")
def tor_refresh(body: TorCountIn = TorCountIn()) -> Dict[str, Any]:
    """Reliable heal: ``rebuild_circuits`` on every running lane, falling back
    to a full ``regenerate_lane`` for ones whose control port is dead (where a
    control-port rebuild can't even connect). This is the "Rebuild lanes" deck
    button -- the deeper, await_build counterpart to NEWNYM.
    """
    lanes = _resolve_tor_lanes(body)
    results: List[Dict[str, Any]] = []
    for lane in lanes:
        if not lane._is_running() or not _port_is_open("127.0.0.1", lane.control_port, timeout=0.3):
            log.info("[tor-refresh] lane #%d not running -- regenerating", lane.index)
            try:
                ok = tor_manager.regenerate_lane(lane, log=log.info)
                results.append({"index": lane.index, "ok": ok, "regenerated": True})
            except Exception as exc:  # noqa: BLE001
                results.append({"index": lane.index, "ok": False, "error": str(exc)})
            continue
        results.append(tor_manager.rebuild_circuits(lane, log=log.info))
    # exit_ip is cleared by rebuild; the health daemon's next sweep refills it
    # and re-syncs the pool. Fresh probes are visible on the next poll.
    return {"results": results, "pool": proxy_pool.status()}


def _resolve_tor_lanes(body: TorCountIn) -> List:
    """Translate the optional ``lane`` field (1-based index) into a 1-element
    list, or all lanes when omitted. Raises 400 on an out-of-range index."""
    if body.lane is None:
        return list(tor_manager.lanes)
    if body.lane < 1 or body.lane > tor_manager.count:
        raise HTTPException(400, f"lane index {body.lane} out of range (1..{tor_manager.count})")
    return [tor_manager.lanes[body.lane - 1]]




@app.get("/api/usage")
def get_usage(display_limit: int = 500, window_minutes: int = 60,
              bucket_seconds: int = 60) -> Dict[str, Any]:
    """Usage summary, recent feed, and both time series.

    ``display_limit`` caps the recent-request feed (UI display only, not a
    rate limit). ``window_minutes`` / ``bucket_seconds`` size the live series;
    the default hour-at-minute-resolution is what lets the dashboard show a
    request seconds after it happens, which the day-granular series cannot.
    """
    return {
        "summary": usage_store.summary(),
        "recent": usage_store.recent(display_limit),
        "daily": usage_store.daily(30),
        "live": usage_store.buckets(window_minutes, bucket_seconds),
        "server_time": time.time(),
    }


@app.get("/api/usage/since/{after_id}")
def get_usage_since(after_id: int, limit: int = 200) -> Dict[str, Any]:
    """Rows newer than ``after_id``, plus refreshed aggregates.

    The dashboard polls this instead of refetching the whole log, so the
    ledger stays live without re-transferring hundreds of rows each tick.
    """
    return {
        "rows": usage_store.since(after_id, limit),
        "summary": usage_store.summary(),
        "live": usage_store.buckets(60, 60),
        "server_time": time.time(),
    }


@app.delete("/api/usage")
def clear_usage() -> Dict[str, Any]:
    """Delete every logged request.

    Irreversible -- the ledger is the only record of past traffic.
    """
    removed = usage_store.reset()
    log.info("usage ledger cleared rows=%d", removed)
    return {"cleared": removed, "summary": usage_store.summary()}


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------
def _call_dispatcher_model(messages: List[Dict[str, Any]], model: str, session_id: str = "") -> str:
    """Run the dispatcher model across providers (itself failover-capable)."""
    provs = catalog.providers_for(model)
    if not provs:
        raise RuntimeError(f"no provider serves dispatcher model '{model}'")
    resp, _prov, _key, _attempts = executor.execute_nonstream(
        messages, model, provs, proxy_pool=proxy_pool, session_id=session_id,
        timeout=config.DISPATCH_TIMEOUT, catalog=catalog,
    )
    return extract_assistant_text(resp)


def _run_dispatcher(messages: List[Dict[str, Any]], had_images: bool, session_id: str = ""):
    def call_model(msgs, mdl):
        return _call_dispatcher_model(msgs, mdl, session_id=session_id)
    try:
        target, reason, _ = dispatcher.decide(
            messages, catalog, call_model, force_images=had_images
        )
        return target, reason, "dispatcher"
    except Exception as exc:  # never let a dispatcher failure drop the request
        # `messages` matters here: without it the fallback ignored the request and
        # returned the router's own model, so a dispatcher outage sent a refactor
        # or an image to whatever DISPATCHER_MODEL happened to be.
        # `exclude={brain}` keeps the burned brain out of its own recovery pool --
        # otherwise a burned brain would be re-handed back as the fallback target,
        # looping the failure. The brain is resolved live (pinned config id or the
        # auto-promoted catalog pick) so the exclusion still means something.
        try:
            brain = dispatcher.dispatcher_model(catalog)
            target = dispatcher.fallback_model(
                catalog, had_images,
                exclude={brain} if brain else set(), messages=messages,
            )
        except dispatcher.DispatcherUnavailable as unavailable:
            # No live model anywhere AND the brain is burned too: surface a 503
            # rather than re-hand a known-dead id to the executor (which would
            # crash on it and recurse back here). The request handler's existing
            # HTTPException pipeline emits the 503 cleanly.
            raise HTTPException(503, str(unavailable)) from exc
        return target, f"dispatcher unavailable: {exc}", "fallback"



# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def _resolve_effort(
    params: Dict[str, Any], target: str, previous: Optional[Any] = None,
) -> Optional[str]:
    """Translate a client's ``reasoning_effort`` into one the target honours.

    Harnesses disagree on how to name thinking depth, and each OpenCode model
    publishes its own set of values it actually implements. Effort therefore
    cannot be resolved until routing has chosen a model, which is why this runs
    here rather than in the request parsers.

    Mutates ``params`` in place. The parameter is dropped entirely when the label
    is unusable *or* when the model exposes no effort control -- OpenCode returns
    200 for a value a model ignores, so forwarding one would look like it worked
    while changing nothing.

    ``previous`` re-resolves against a *different* model on the failover path.
    Without it the already-clamped value for the primary model would be re-clamped
    as if the client had asked for it: `max` clamped to deepseek's `max`, then
    carried unchanged onto ling, which does not implement it. Passing the original
    label makes the second resolution independent of the first.

    Returns the value actually sent, for logging.
    """
    requested = params.pop("reasoning_effort", None)
    if previous is not None:
        requested = previous
    if requested is None:
        return None
    lm = catalog.by_id(target)
    caps = getattr(lm, "capabilities", None) or {}
    allowed = caps.get("effort")
    resolved = effort.resolve(requested, allowed)
    if resolved is None:
        log.info(
            "effort: dropped %r for %s (honours %s)",
            requested, target, allowed or "no effort control",
        )
        return None
    if resolved != requested:
        log.info("effort: %r -> %r for %s", requested, resolved, target)
    params["reasoning_effort"] = resolved
    return resolved


def _messages_for_model(
    messages: List[Dict[str, Any]], model_id: str, has_images: bool,
) -> List[Dict[str, Any]]:
    """Make ``messages`` safe for ``model_id``, stripping images it cannot see.

    ``dispatcher.fallback_model`` answers an image request from a text-only
    model when every vision-capable one is down or burning. A text-only model
    cannot read image parts -- OpenCode answers HTTP 400 -- so every fallback
    site replaces them with the same placeholder the primary-target path uses
    (see ``vision_bridge.strip_images_for_text_model``). Idempotent, and a
    no-op for a vision-capable model or a text-only request.
    """
    if not has_images:
        return messages
    lm = catalog.by_id(model_id)
    if lm is not None and not lm.vision:
        return vision_bridge.strip_images_for_text_model(messages)
    return messages


# Muse Spark's upstream intermittently completes a Responses turn with zero
# output items and zero usage (the ledger shows 41 such rows, all logged
# ``ok_stream``, up to four in a row). The old fix -- buffer the whole stream,
# detect blank, silently retry with downgraded effort or a fallback model --
# made the client stare at silence for the entire first attempt (up to 67s),
# reopened an exhausted generator on the retry path, and could still flush the
# empty buffer. The retry ladder below replaces it: re-run the request up to
# three times on blank, each attempt a fresh egress pick, and if every attempt
# comes back empty say so plainly instead of pretending the model answered.
_RESPONSES_BLANK_MAX_ATTEMPTS = 3


def _escalate_output_budget(params: Dict[str, Any], next_attempt: int) -> Dict[str, Any]:
    """Double the output budget for a blank retry so a budget-clipped think
    phase gets room to finish.

    Live wire capture: when a reasoning turn exhausts ``max_output_tokens``
    the upstream ends the turn mid-think (``response.incomplete`` with
    ``reason: "max_output_tokens"``, no completed reasoning item, no
    continuation state) -- re-sending the same request with the same cap
    re-blanks identically (verified at 512/1024/2048; the same prompt
    completes at 8192). Escalating the cap is the only lever that turns
    that blank into an answer. ``next_attempt`` is the 1-based attempt
    number that will use the escalated budget, so the first retry doubles
    the client's cap, the second quadruples it, and so on, bounded by the
    model's published output headroom.
    """
    escalated = dict(params)
    current = escalated.get("max_tokens")
    if not (isinstance(current, int) and current > 0):
        current = escalated.get("max_completion_tokens")
    if not (isinstance(current, int) and current > 0):
        return escalated
    escalated["max_tokens"] = min(
        current * (2 ** (next_attempt - 1)), _MAX_OUTPUT_HEADROOM,
    )
    return escalated


def _responses_blank_notice(model: str, attempts: int) -> str:
    """The honest terminal for a turn that stayed empty across every retry."""
    return (
        f"[lingling: {model} returned an empty response {attempts} times in a "
        f"row. The upstream is intermittently blank; retry, or pick another model.]"
    )


def _responses_output_blank(out: Dict[str, Any]) -> bool:
    """True when a translated Responses object carries nothing a client can use.

    ``output`` is the only thing Codex renders; a response whose output is
    missing, empty, or holds only reasoning items (no message, no tool call)
    reads as an empty turn no matter what the upstream reported.
    """
    for item in out.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            return False
        if item.get("type") == "function_call":
            return False
    return True


async def _responses_blank_retry(
    messages: List[Dict[str, Any]],
    target: str,
    target_providers: List[Any],
    params: Dict[str, Any],
    *,
    session_id: str,
    had_images: bool,
    requested: str,
    original_effort: Optional[str],
    attempt_proxy_ref: Optional[Dict[str, Any]] = None,
    max_attempts: int = _RESPONSES_BLANK_MAX_ATTEMPTS,
) -> Optional[Dict[str, Any]]:
    """Re-run a non-stream Responses request until it produces output.

    Returns the first non-blank translated response, or None when every
    attempt came back blank. A blank that carries completed reasoning items
    (``lingling_resume_items``) is the model's own think-phase boundary, not
    upstream intermittency: the retry resumes the model's thinking by feeding
    those items back as input, pinned to the same egress lane that served the
    blank (``attempt_proxy_ref``). Only a blank with no continuation state
    re-runs the request on a fresh exit IP under the pool's normal policy.
    ``params`` already carries the effort resolved for ``target``, so attempts
    reuse it unchanged. The caller logs the outcome; this helper never raises.
    """
    base_params = dict(params)
    for attempt in range(1, max_attempts + 1):
        try:
            resp, prov, key, _attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                _messages_for_model(messages, target, had_images),
                target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, catalog=catalog, **params
            )
        except (executor.AllFailedError, executor.NoProviderError) as exc:
            log.warning(
                "responses blank retry attempt %d/%d failed to start: %s",
                attempt, max_attempts, getattr(exc, "last_error", exc),
            )
            return None
        out = responses_bridge.response_object(resp, requested, target, prov.id)
        if not _responses_output_blank(out):
            return out
        resume_items = resp.get("lingling_resume_items") or []
        if resume_items:
            # The model's own think-phase boundary, not an upstream failure:
            # it completed a reasoning item (encrypted continuation state) and
            # ended the turn without visible text. It will think again on its
            # own -- resume that thinking by feeding the completed reasoning
            # item back as input, on the same egress lane that just served it
            # (rotation is what burned lanes and queued regenerations in the
            # health daemon).
            log.info(
                "responses think-phase boundary attempt %d/%d target=%s effort=%s -- "
                "model ended its turn after reasoning with no visible text; "
                "resuming its thinking via encrypted reasoning on the same lane",
                attempt, max_attempts, target,
                params.get("reasoning_effort") or original_effort or "",
            )
            try:
                resume_params = dict(params)
                resume_params["resume_items"] = resume_items
                resp, prov, key, _attempts = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages, target, had_images),
                    target, target_providers, proxy_pool=proxy_pool,
                    session_id=session_id, catalog=catalog,
                    pin_proxy_id=(
                        attempt_proxy_ref.get("id") if attempt_proxy_ref else None
                    ), **resume_params
                )
            except (executor.AllFailedError, executor.NoProviderError) as exc:
                log.warning(
                    "responses blank resume attempt %d/%d failed to start: %s",
                    attempt, max_attempts, getattr(exc, "last_error", exc),
                )
                return None
            out = responses_bridge.response_object(resp, requested, target, prov.id)
            if not _responses_output_blank(out):
                return out
            log.warning(
                "responses blank after resume attempt %d/%d target=%s -- "
                "falling through to fresh-egress retry",
                attempt, max_attempts, target,
            )
            continue
        if attempt_proxy_ref and attempt_proxy_ref.get("id"):
            # No continuation state to resume (the upstream cut the turn off
            # mid-think without finalizing a reasoning item). The model will
            # think again on its own -- re-run the same request on the same
            # lane rather than rotating: the lane is not the problem, and
            # rotation is what burned lanes and queued regenerations in the
            # health daemon.
            log.info(
                "responses think-phase boundary attempt %d/%d target=%s effort=%s -- "
                "model ended its turn mid-think with no visible text and no "
                "continuation state; re-running the same request on the same lane",
                attempt, max_attempts, target,
                params.get("reasoning_effort") or original_effort or "",
            )
            try:
                # Same cap re-blanks identically (budget-clipped think phase) --
                # escalate so the think gets room to finish.
                same_params = _escalate_output_budget(base_params, attempt + 1)
                resp, prov, key, _attempts = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages, target, had_images),
                    target, target_providers, proxy_pool=proxy_pool,
                    session_id=session_id, catalog=catalog,
                    pin_proxy_id=attempt_proxy_ref.get("id"), **same_params
                )
            except (executor.AllFailedError, executor.NoProviderError) as exc:
                log.warning(
                    "responses blank same-lane retry attempt %d/%d failed to start: %s",
                    attempt, max_attempts, getattr(exc, "last_error", exc),
                )
                return None
            out = responses_bridge.response_object(resp, requested, target, prov.id)
            if not _responses_output_blank(out):
                return out
            log.warning(
                "responses blank after same-lane retry attempt %d/%d target=%s -- "
                "falling through to fresh-egress retry",
                attempt, max_attempts, target,
            )
            continue
        log.warning(
            "responses blank attempt %d/%d target=%s effort=%s -- upstream "
            "completed with no output; retrying on a fresh egress",
            attempt, max_attempts, target,
            params.get("reasoning_effort") or original_effort or "",
        )
        # No continuation state: the upstream cut the turn off mid-think
        # (budget-clipped). Re-sending the same request with the same cap
        # re-blanks identically -- escalate the output budget so the think
        # phase gets room to finish.
        params = _escalate_output_budget(base_params, attempt + 1)
    return None


def _is_responses_only_model(model_id: str) -> bool:
    """Whether ``model_id`` is served only on the Responses API.

    The same capability flag that routes the request to ``/v1/responses``
    (``providers.opencode.is_responses_model``) marks the models whose
    upstream can complete a turn with zero visible content -- hidden
    reasoning tokens, no content delta. The chat blank-turn retry ladder
    keys off this instead of a hardcoded id, so a new Responses-only model
    arriving upstream gets the same recovery without a code change.
    Operators can extend the set at runtime via ``LINGLING_RESPONSES_MODELS``;
    the ladder follows automatically.
    """
    prov = providers.get("opencode")
    check = getattr(prov, "is_responses_model", None) if prov is not None else None
    return bool(check and check(model_id))


async def _execute_with_egress_wait(fn, *args, **kwargs):
    """Run an executor call, waiting out a fully-cooled egress pool once.

    The executor is synchronous, so it always goes through the threadpool. When
    it reports that every attempt failed, this asks the proxy pool whether the
    failure was exhaustion -- every exit in cooldown -- and if so holds the
    request until the soonest exit returns, then tries once more.

    ``parking.wait_for_egress`` returns 0 when waiting cannot help (an exit is
    already free, there is no pool, or the wait exceeds the budget), and then the
    original ``AllFailedError`` propagates and the caller behaves exactly as it
    did before parking existed -- model fallback included.
    """
    # Use the proxy_pool the caller passed, not the module-level one. The
    # executor receives it via kwargs; the egress-wait check must consult the
    # same pool or it can wait against an empty/wrong one (most visible in
    # tests, where a custom pool is passed but the module-level var was used).
    pool = kwargs.get("proxy_pool", proxy_pool)
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except executor.AllFailedError:
        waited = await parking.wait_for_egress(pool, config.EGRESS_WAIT_BUDGET, log)
        if not waited:
            raise
        log.info("egress: waited %.1fs for a fresh exit, retrying the request", waited)
        return await run_in_threadpool(fn, *args, **kwargs)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_started = time.time()
    client = request.client.host if request.client else "unknown"

    try:
        body = await request.json()
    except JSONDecodeError as exc:
        log.warning("chat invalid_json client=%s error=%s", client, exc)
        raise HTTPException(400, "Invalid JSON request body.")
    if not isinstance(body, dict):
        # A bare list/string/null is well-formed JSON but not a request. Every
        # field read below assumes a mapping, so this became an AttributeError
        # inside the handler and surfaced as a 500 rather than a 400.
        raise HTTPException(400, "Request body must be a JSON object.")

    requested = body.get("model")
    if not isinstance(requested, str) or not requested:
        raise HTTPException(400, "Missing or invalid 'model' field.")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "Missing or invalid 'messages' field.")
    if not all(isinstance(m, dict) for m in messages):
        raise HTTPException(400, "Each entry in 'messages' must be an object.")

    stream = bool(body.get("stream"))
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""
    had_images = vision_bridge.messages_have_images(messages)

    # Decide target model.
    if requested == config.MULTIMODEL_ID:
        # _run_dispatcher calls the dispatcher model over sync httpx and can take
        # seconds; off the event loop it goes, same as the executor below.
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages, had_images, session_id
        )
    else:
        target = requested
        if not catalog.providers_for(target):
            # Allow provider-prefixed ids such as opencode/deepseek-v4-flash-free.
            bare = target.split("/", 1)[1] if "/" in target else target
            if bare != target and catalog.providers_for(bare):
                target = bare
            else:
                raise HTTPException(400, f"Unknown or unsupported model: {requested!r}")
        reason = "user requested"
        routed_by = "user"
        # The client explicitly named a model the recycler has retired.
        # Both burned AND blacklisted (including retired-seed) are treated as
        # unavailable — reroute to the nearest live substitute so the request
        # still goes through. This fixes muse-spark explicit picks that were
        # blacklisted but not burned: previously they bypassed this check and
        # were dispatched to an upstream that dies mid-flight, leaving Codex
        # with an empty reply.
        if catalog.is_burned(target) or catalog.is_blacklisted(target):
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            if not (fallback and fallback != target and catalog.providers_for(fallback)):
                raise HTTPException(
                    503, f"explicit pick {requested!r} is unavailable, no live substitute",
                )
            log.info("reroute %s -> %s (explicit pick unavailable)", target, fallback)
            target = fallback
            reason = f"explicit pick {requested!r} was burned; rerouted by recycler"
            routed_by = "recycler-reroute"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(503, f"No provider available for model '{target}'.")

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)

    params = _passthrough_params(body)
    original_effort = params.get("reasoning_effort")
    _resolve_effort(params, target)

    # Which of the three wire entrypoints this turn came through, for the ledger
    # (a 429 egress burn and a 400 shape rejection read identically without it).
    call_type = "chat"

    routing_meta = {
        "requested_model": requested,
        "routed_model": target,
        "routed_by": routed_by,
        "reason": reason,
    }

    # Non-streaming path.
    if not stream:
        started = time.time()
        # The lane that served the first attempt. A blank turn is the model's
        # own think-phase boundary, not an egress failure, so the resume retry
        # pins back onto this same lane instead of rotating (rotation is what
        # burned lanes and queued regenerations in the health daemon).
        attempt_proxy_ref: Dict[str, Any] = {}
        try:
            # The executor and the provider beneath it are synchronous
            # (httpx.Client), so awaiting them on the event loop would block
            # every other request for the whole upstream call.
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, catalog=catalog,
                proxy_ref=attempt_proxy_ref, **params
            )
        except executor.AllFailedError as exc:
            # Model-class burning is already done by ``executor`` at the point
            # of failure (so chat-stream, which never falls back, still
            # recycles). Falling through to a different model is the chat
            # path's job -- the executor only knows about target.
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            fallback_providers = catalog.providers_for(fallback)
            if fallback and fallback != target and fallback_providers:
                try:
                    retry_params = dict(params)
                    # Each model publishes its own legal effort values, so the
                    # primary's clamped value is re-resolved from the client's
                    # original label rather than carried across untouched.
                    _resolve_effort(retry_params, fallback, previous=original_effort)
                    resp, prov, key, attempts2 = await _execute_with_egress_wait(
                        executor.execute_nonstream,
                        _messages_for_model(messages, fallback, had_images),
                        fallback, fallback_providers, proxy_pool=proxy_pool,
                        session_id=session_id, catalog=catalog, **retry_params
                    )
                    attempts = exc.attempts + attempts2
                    target = fallback
                    reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
                    routed_by = "fallback"
                    routing_meta.update(
                        {"routed_model": target, "routed_by": routed_by, "reason": reason}
                    )
                except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                    usage_store.log(
                        requested, target, routed_by, reason, status="exhausted",
                        had_images=had_images,
                        error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                        call_type=call_type,
                        attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                        last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                        rerouted=(routed_by == "fallback"),
                    )
                    raise HTTPException(503, f"All providers exhausted for '{target}'.")
            else:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                    call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")

        latency = (time.time() - started) * 1000.0
        usage = extract_usage(resp)
        account_id = key.id if key is not None else None  # keyless -> no account
        usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=latency, status="ok", had_images=had_images,
            account_id=account_id, provider=prov.id,
            call_type=call_type, attempts=len(attempts),
            last_upstream_status=None, rerouted=(routed_by == "fallback"),
        )
        resp["lingling"] = {
            **routing_meta, "provider": prov.id, "account": account_id, "attempts": attempts,
        }
        log.info(
            "%s  ----  %dms",
            target, int((time.time() - request_started) * 1000.0),
        )
        # A reasoning-only upstream (nemotron-style: content=="" with
        # reasoning/reasoning_content/thinking populated) would otherwise reach
        # an OpenAI chat client as a blank turn. Promote the first non-empty
        # reasoning field into message.content so the answer is usable; the
        # raw reasoning fields stay intact for clients that understand them.
        # See providers.base.promote_reasoning_to_content.
        if promote_reasoning_to_content(resp):
            log.info(
                "chat blank-turn recovery requested=%s target=%s provider=%s "
                "stream=false -- upstream returned empty content, surfaced reasoning",
                requested, target, prov.id,
            )
        # A Responses-only upstream (muse-spark-style: hidden reasoning
        # tokens, zero visible content) can complete a turn blank even after
        # promotion. If still empty, surface Lingling's notice and retry
        # once (downgrade effort or fallback model) instead of returning
        # blank. Keyed off the same capability flag that routes the model to
        # /v1/responses, so any new Responses-only model gets the recovery
        # without a code change.
        choices = resp.get("choices") or [{}]
        msg = (choices[0].get("message") if isinstance(choices[0], dict) else {}) or {}
        is_empty = not (msg.get("content") or msg.get("tool_calls"))
        if is_empty:
            is_spark = _is_responses_only_model(target)
            eff = str(params.get("reasoning_effort") or original_effort or "")
            log.warning(
                "chat empty after promote requested=%s target=%s effort=%s provider=%s — %s",
                requested, target, eff, prov.id,
                "Responses-only model has an empty reply at xhigh" if is_spark and eff.lower() in ("xhigh","max","ultra") else "blank turn",
            )
            retried = False
            # The model's own think-phase boundary, not an upstream failure:
            # it completed a reasoning item (encrypted continuation state) and
            # ended the turn without visible text. It will think again on its
            # own -- resume that thinking by feeding the completed reasoning
            # item back as input, on the same egress lane that just served it
            # (rotation is what burned lanes and queued regenerations in the
            # health daemon). Only fall through to downgrade/fallback if the
            # resume itself comes back blank.
            resume_items = resp.get("lingling_resume_items") or []
            if resume_items:
                log.info(
                    "chat think-phase boundary requested=%s target=%s effort=%s -- "
                    "model ended its turn after reasoning with no visible text; "
                    "resuming its thinking via encrypted reasoning on the same lane",
                    requested, target, eff,
                )
                try:
                    resume_params = dict(params)
                    resume_params["resume_items"] = resume_items
                    resp2, prov2, key2, attempts2 = await _execute_with_egress_wait(
                        executor.execute_nonstream,
                        _messages_for_model(messages, target, had_images),
                        target, target_providers, proxy_pool=proxy_pool,
                        session_id=session_id, catalog=catalog,
                        pin_proxy_id=attempt_proxy_ref.get("id"), **resume_params
                    )
                    if promote_reasoning_to_content(resp2):
                        log.info("chat resume succeeded via promote")
                    c2 = (resp2.get("choices") or [{}])[0].get("message") or {}
                    if c2.get("content") or c2.get("tool_calls"):
                        resp = resp2
                        prov = prov2
                        key = key2
                        attempts = attempts + attempts2
                        retried = True
                        log.info("chat resume succeeded target=%s", target)
                except Exception as resume_exc:
                    log.warning("chat resume failed: %s -- falling through to downgrade/fallback", resume_exc)
            elif attempt_proxy_ref.get("id"):
                # No continuation state to resume (the upstream cut the turn
                # off mid-think without finalizing a reasoning item). The model
                # will think again on its own -- re-run the same request on the
                # same lane rather than rotating: the lane is not the problem,
                # and rotation is what burned lanes and queued regenerations
                # in the health daemon.
                log.info(
                    "chat think-phase boundary requested=%s target=%s effort=%s -- "
                    "model ended its turn mid-think with no visible text and no "
                    "continuation state; re-running the same request on the same lane",
                    requested, target, eff,
                )
                try:
                    # Same cap re-blanks identically (budget-clipped think
                    # phase) -- escalate so the think gets room to finish.
                    same_params = _escalate_output_budget(params, 2)
                    resp2, prov2, key2, attempts2 = await _execute_with_egress_wait(
                        executor.execute_nonstream,
                        _messages_for_model(messages, target, had_images),
                        target, target_providers, proxy_pool=proxy_pool,
                        session_id=session_id, catalog=catalog,
                        pin_proxy_id=attempt_proxy_ref.get("id"), **same_params
                    )
                    if promote_reasoning_to_content(resp2):
                        log.info("chat same-lane retry succeeded via promote")
                    c2 = (resp2.get("choices") or [{}])[0].get("message") or {}
                    if c2.get("content") or c2.get("tool_calls"):
                        resp = resp2
                        prov = prov2
                        key = key2
                        attempts = attempts + attempts2
                        retried = True
                        log.info("chat same-lane retry succeeded target=%s", target)
                except Exception as same_exc:
                    log.warning("chat same-lane retry failed: %s -- falling through to downgrade/fallback", same_exc)
            if not retried:
                # Try once: downgrade effort on same model if high effort, else fallback model.
                downgrade_map = {"xhigh": "high", "max": "xhigh", "ultra": "max", "high": "medium"}
            downgraded = downgrade_map.get(eff.lower()) if eff else None
            retried = False
            if downgraded and is_spark:
                try:
                    retry_params = dict(params)
                    retry_params["reasoning_effort"] = downgraded
                    _resolve_effort(retry_params, target)  # re-clamp to model's allowed
                    resp2, prov2, key2, attempts2 = await _execute_with_egress_wait(
                        executor.execute_nonstream,
                        _messages_for_model(messages, target, had_images),
                        target, target_providers, proxy_pool=proxy_pool,
                        session_id=session_id, catalog=catalog, **retry_params
                    )
                    if promote_reasoning_to_content(resp2):
                        log.info("chat empty retry downgraded %s -> %s succeeded via promote", eff, downgraded)
                    c2 = (resp2.get("choices") or [{}])[0].get("message") or {}
                    if c2.get("content") or c2.get("tool_calls"):
                        resp = resp2
                        prov = prov2
                        key = key2
                        attempts = attempts + attempts2
                        retried = True
                        log.info("chat empty retry downgraded %s -> %s target=%s succeeded", eff, downgraded, target)
                except Exception as retry_exc:
                    log.warning("chat empty retry downgraded failed: %s", retry_exc)
            if not retried:
                # Fallback model path (also covers non-spark empties)
                fallback = dispatcher.fallback_model(catalog, had_images, exclude={target}, messages=messages)
                fb_provs = catalog.providers_for(fallback) if fallback else None
                if fallback and fb_provs:
                    try:
                        retry_params = dict(params)
                        _resolve_effort(retry_params, fallback, previous=original_effort)
                        resp2, prov2, key2, attempts2 = await _execute_with_egress_wait(
                            executor.execute_nonstream,
                            _messages_for_model(messages, fallback, had_images),
                            fallback, fb_provs, proxy_pool=proxy_pool,
                            session_id=session_id, catalog=catalog, **retry_params
                        )
                        if promote_reasoning_to_content(resp2):
                            log.info("chat empty fallback %s -> %s promoted reasoning", target, fallback)
                        c2 = (resp2.get("choices") or [{}])[0].get("message") or {}
                        if c2.get("content") or c2.get("tool_calls"):
                            resp = resp2
                            prov = prov2
                            target = fallback
                            reason = f"empty turn on {requested}; fell back to {fallback}"
                            routed_by = "empty-fallback"
                            retried = True
                            log.info("chat empty fallback %s -> %s succeeded", requested, fallback)
                    except Exception as fb_exc:
                        log.warning("chat empty fallback failed: %s", fb_exc)
            if not retried:
                log.info("chat empty still blank after retry — returning without notice per user request (empty will be retried silently on next turn) target=%s", target)
                # No notice injected — per latest user request the empty message
                # must never reach the CLI; the next turn's retry will handle it.
                # Keep resp as is (empty) but don't surface a synthetic message.
            # Refresh lingling meta after any retry/fallback injection
            resp["lingling"] = {
                **routing_meta, "provider": prov.id, "account": key.id if key else None, "attempts": attempts,
                "routed_model": target, "routed_by": routed_by, "reason": reason,
            }
        return JSONResponse(resp)

    # Streaming path: open the upstream SSE connection and wait for the first
    # chunk so dead proxies can fail over before we return HTTP 200. After that,
    # the bytes are forwarded untouched, guarded by a single mid-flight retry.
    #
    # Recovery re-emits the whole answer after a reset marker, so a client that
    # ignores the marker would render it twice. Callers can opt out per request.
    recover = bool(body.get("lingling_recover", config.STREAM_RECOVERY))
    started = time.time()
    # The lane that served the first attempt. A blank turn is the model's own
    # think-phase boundary, not an egress failure, so the resume retry pins
    # back onto this same lane instead of rotating (rotation is what burned
    # lanes and queued regenerations in the health daemon).
    attempt_proxy_ref: Dict[str, Any] = {}
    try:
        # execute_stream blocks until the first upstream chunk arrives (up to
        # STREAM_FIRST_TOKEN_TIMEOUT), so it cannot run on the event loop. The
        # generator it returns is safe: StreamingResponse iterates sync
        # iterators in a worker thread already.
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog,
            proxy_ref=attempt_proxy_ref, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        # Parity with the non-stream path and the Responses stream path: a 429-
        # or 5xx-burn across all egress lanes for the primary model should not
        # surface as an empty-stream 503 when another free model is live. This
        # was the root of "empty reply from all models" via chat streaming
        # (Cline/dashboard) while non-stream fallback succeeded — the stream
        # path had no pre-200 fallback at all, so a transient 429 storm
        # produced a 503 that the client rendered as empty.
        fallback = dispatcher.fallback_model(
            catalog, had_images, exclude={target}, messages=messages,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if fallback and fallback != target and fallback_providers:
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                retry_msgs = _messages_for_model(messages, fallback, had_images)
                stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                    executor.execute_stream,
                    retry_msgs, fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **retry_params
                )
                attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
                target = fallback
                reason = f"primary stream failed ({getattr(exc, 'last_error', exc)}); fell back to {fallback}"
                routed_by = "fallback"
                routing_meta.update({"routed_model": target, "routed_by": routed_by, "reason": reason})
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                error = str(getattr(fallback_exc, "last_error", fallback_exc))
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=error[:300],
                    call_type=call_type,
                    attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")
        else:
            error = str(getattr(exc, "last_error", exc))
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
                call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                rerouted=(routed_by == "fallback"),
            )
            raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

    account_id = key.id if key is not None else None  # keyless -> no account
    # Log at first chunk so a stream that dies mid-flight still leaves a record;
    # finalize() fills in the token counts once the terminal usage chunk lands.
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True, call_type=call_type, attempts=len(attempts),
        last_upstream_status=None, rerouted=(routed_by == "fallback"),
    )
    log.info(
        "%s  ----  firstchunk %dms",
        target, int((time.time() - request_started) * 1000.0),
    )

    # Mid-stream recovery may land on a different model, but only when the client
    # asked the router to choose. A request that named `deepseek-v4-flash-free`
    # gets that model on the retry too; `lingling-auto` delegated the choice, so
    # the retry is free to re-decide -- and re-decoding matters because the usual
    # reason a stream dies mid-flight is the model itself stalling, and reopening
    # on the same one spends the single retry on the thing that just failed.
    auto_routed = requested == config.MULTIMODEL_ID
    reroute = {"model": target, "reason": reason, "by": routed_by}

    def _reopen():
        """Open a replacement upstream stream for mid-flight recovery.

        Goes back through the executor, so the retry picks a fresh exit IP under
        the pool's normal policy rather than reusing the one that just died. For
        an auto-routed turn it also picks a fresh *model*, excluding the one that
        broke. Only the generator is kept; the chosen model is recorded on
        `reroute` so the ledger and the log can report where the answer came from.
        """
        retry_model = target
        retry_params = params
        if auto_routed:
            alternative = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            alt_providers = catalog.providers_for(alternative)
            if alternative and alternative != target and alt_providers:
                retry_model = alternative
                # Effort is per-model, so it is re-resolved from the client's
                # original label rather than carried across from the dead model.
                retry_params = dict(params)
                _resolve_effort(retry_params, retry_model, previous=original_effort)
                reroute.update({
                    "model": retry_model,
                    "reason": f"stream broke on {target}; rerouted to {retry_model}",
                    "by": "reroute",
                })
                log.warning(
                    "chat stream rerouting mid-flight: %s -> %s", target, retry_model
                )
        providers_for_retry = catalog.providers_for(retry_model) or target_providers
        # A text-only retry target cannot see the images the stalled vision
        # model could; swap them for the same placeholder the non-streaming
        # fallback uses, or OpenCode answers 400 and the retry is wasted.
        retry_messages = _messages_for_model(messages, retry_model, had_images)
        again, _prov, _key, _attempts = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
            catalog=catalog, **retry_params
        )
        return stream_idle.with_idle_timeout(again, config.STREAM_IDLE_TIMEOUT, log)

    def _hold_for_egress():
        """Wait for a free exit before the mid-stream retry reopens.

        Same decision as the pre-first-token wait, but this one runs inside a
        live SSE response, so it emits keepalive comments instead of awaiting
        silently. Yields nothing when waiting cannot help.
        """
        yield from parking.hold_stream_for_egress(
            proxy_pool, config.EGRESS_WAIT_BUDGET, log,
        )

    def event_stream():
        # First bit goes to the client immediately: some chat SDKs (Codex CLI
        # in particular, and any SSE client with a read-deadline under
        # STREAM_FIRST_TOKEN_TIMEOUT) drop a streaming connection that hasn't
        # produced bytes within a few seconds, even though Lingling is working
        # through proxy rotation. A one-shot SSE comment line is invisible to
        # the chat model but keeps the connection warm until first chunk.
        yield b": lingling heartbeat -- streaming start for target=" + target.encode() + b"\n\n"
        # OpenCode's SSE bytes are forwarded verbatim -- no re-spacing, no field
        # mirroring, no reasoning rewriting. Two things are layered on top:
        #   1. each frame is *read* in passing to harvest token counts, without
        #      which every streamed request records zero usage;
        #   2. a stream that dies before the model reported completion is
        #      retried once on a fresh exit IP (see stream_guard).
        seen: Dict[str, Any] = {}
        outcome = stream_guard.StreamOutcome()
        try:
            # Buffer the first stream so an empty never reaches the CLI.
            # Per latest user request: "that message will never reach the user,
            # Lingling will just rerun until it actually starts streaming."
            buf: list[bytes] = []
            for chunk in stream_with_blank_chat_recovery(
                stream_guard.guarded_stream(
                    open_stream=_reopen,
                    first=stream_idle.with_idle_timeout(
                        stream_iter, config.STREAM_IDLE_TIMEOUT, log),
                    outcome=outcome,
                    on_chunk=lambda raw: _harvest_stream_usage(raw, seen),
                    log=log,
                    enabled=recover,
                    hold=_hold_for_egress,
                    retry_model=lambda: (
                        reroute["model"] if reroute["model"] != target else None
                    ),
                ),
                outcome, seen, requested, target, log,
            ):
                buf.append(chunk)
            # Any visible content? A Responses-only model (muse-spark-style:
            # hidden reasoning tokens) can stream a whole turn with visible==0
            # even though reasoning may be present — treat any visible==0 as
            # empty and retry silently. A tool-call turn is never empty: the
            # client is waiting on those arguments, not on text.
            is_empty = seen.get("_visible_content_chars", 0) == 0 and seen.get("_tool_calls", 0) == 0
            # Retry on any empty, not just xhigh — user confirmed minimal also blanks,
            # so the culprit is response pattern/translation, not effort. Any
            # Responses-only model empty at any effort should be retried silently.
            is_spark = _is_responses_only_model(target)
            eff = str(params.get("reasoning_effort") or original_effort or "")
            should_retry = is_empty and is_spark
            if should_retry:
                resume_items = seen.get("_resume_items") or []
                if resume_items:
                    # The model's own think-phase boundary, not an upstream
                    # failure: it completed a reasoning item (encrypted
                    # continuation state) and ended the turn without visible
                    # text. It will think again on its own -- resume that
                    # thinking by feeding the completed reasoning item back as
                    # input. Pin to the same egress lane that just served it
                    # ONLY when the stream ended cleanly; a stream that broke
                    # (504, connection refused, idle stall) makes the lane
                    # suspect, and pinning a dead lane wastes the attempt.
                    log.info(
                        "chat stream think-phase boundary target=%s effort=%s -- "
                        "model ended its turn after reasoning with no visible "
                        "text; resuming its thinking via encrypted reasoning "
                        "on the same lane",
                        target, eff,
                    )
                    try:
                        resume_params = dict(params)
                        resume_params["resume_items"] = resume_items
                        resume_iter, _, _, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id,
                            timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, pin_proxy_id=(
                                attempt_proxy_ref.get("id")
                                if not outcome.error and not outcome.recovered
                                else None
                            ),
                            **resume_params,
                        )
                        resume_seen: Dict[str, Any] = {}
                        resume_outcome = stream_guard.StreamOutcome()
                        def _resume_reopen():
                            # Mid-flight recovery must land on a fresh exit IP
                            # (guarded_stream's contract) -- the lane that just
                            # broke is suspect, never pin it.
                            fresh, _, _, _ = executor.execute_stream(
                                _messages_for_model(messages, target, had_images),
                                target, target_providers, proxy_pool=proxy_pool,
                                session_id=session_id,
                                timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                                catalog=catalog, **resume_params,
                            )
                            return stream_idle.with_idle_timeout(
                                fresh, config.STREAM_IDLE_TIMEOUT, log)
                        for chunk in stream_guard.guarded_stream(
                            open_stream=_resume_reopen,
                            first=stream_idle.with_idle_timeout(
                                resume_iter, config.STREAM_IDLE_TIMEOUT, log),
                            outcome=resume_outcome,
                            on_chunk=lambda raw: _harvest_stream_usage(raw, resume_seen),
                            log=log,
                            enabled=recover,
                            hold=_hold_for_egress,
                            retry_model=lambda: None,
                        ):
                            yield chunk
                        # The resumed thinking produced visible content: the
                        # model just needed its own continuation. Merge usage
                        # and return -- no downgrade or fallback needed.
                        if resume_seen.get("_visible_content_chars", 0) > 0 or resume_seen.get("_tool_calls", 0) > 0:
                            seen.update(resume_seen)
                            outcome.recovered = True
                            outcome.rescue_succeeded = True
                            outcome.completed = resume_outcome.completed or outcome.completed
                            return
                        # Still blank after resuming -- fall through to the
                        # fresh-lane / downgrade / fallback ladder below.
                        log.warning(
                            "chat stream blank after resume -- "
                            "falling through to fresh-lane retry"
                        )
                    except Exception as resume_exc:
                        log.warning(
                            "chat stream resume retry failed: %s "
                            "-- falling through to fresh-lane retry", resume_exc,
                        )
                elif attempt_proxy_ref.get("id") and not outcome.error and not outcome.recovered:
                    # No continuation state to resume (the upstream cut the
                    # turn off mid-think without finalizing a reasoning item).
                    # The model will think again on its own -- re-run the same
                    # request on the same lane rather than rotating: the lane
                    # is not the problem, and rotation is what burned lanes
                    # and queued regenerations in the health daemon. Only when
                    # the stream ended cleanly -- a broken stream (504,
                    # connection refused, idle stall) makes the lane suspect,
                    # and pinning a dead lane wastes the attempt.
                    log.info(
                        "chat stream think-phase boundary target=%s effort=%s -- "
                        "model ended its turn mid-think with no visible text "
                        "and no continuation state; re-running the same request "
                        "on the same lane",
                        target, eff,
                    )
                    try:
                        # Same cap re-blanks identically (budget-clipped think
                        # phase) -- escalate so the think gets room to finish.
                        same_params = _escalate_output_budget(params, 2)
                        same_iter, _, _, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id,
                            timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, pin_proxy_id=attempt_proxy_ref.get("id"),
                            **same_params,
                        )
                        same_seen: Dict[str, Any] = {}
                        same_outcome = stream_guard.StreamOutcome()
                        def _same_lane_reopen():
                            # Mid-flight recovery must land on a fresh exit IP
                            # (guarded_stream's contract) -- the lane that just
                            # broke is suspect, never pin it.
                            fresh, _, _, _ = executor.execute_stream(
                                _messages_for_model(messages, target, had_images),
                                target, target_providers, proxy_pool=proxy_pool,
                                session_id=session_id,
                                timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                                catalog=catalog, **params,
                            )
                            return stream_idle.with_idle_timeout(
                                fresh, config.STREAM_IDLE_TIMEOUT, log)
                        for chunk in stream_guard.guarded_stream(
                            open_stream=_same_lane_reopen,
                            first=stream_idle.with_idle_timeout(
                                same_iter, config.STREAM_IDLE_TIMEOUT, log),
                            outcome=same_outcome,
                            on_chunk=lambda raw: _harvest_stream_usage(raw, same_seen),
                            log=log,
                            enabled=recover,
                            hold=_hold_for_egress,
                            retry_model=lambda: None,
                        ):
                            yield chunk
                        if same_seen.get("_visible_content_chars", 0) > 0 or same_seen.get("_tool_calls", 0) > 0:
                            seen.update(same_seen)
                            outcome.recovered = True
                            outcome.rescue_succeeded = True
                            outcome.completed = same_outcome.completed or outcome.completed
                            return
                        log.warning(
                            "chat stream blank on same lane too -- "
                            "falling through to fresh-lane retry"
                        )
                    except Exception as same_exc:
                        log.warning(
                            "chat stream same-lane retry failed: %s "
                            "-- falling through to fresh-lane retry", same_exc,
                        )
                has_reasoning = bool(seen.get("_reasoning_text", "").strip()) or (seen.get("reasoning_tokens", 0) or 0) > 0
                if has_reasoning:
                    # The model was mid-think when the upstream closed the
                    # stream (reasoning frames present, zero visible content).
                    # A fresh lane gives the same model another shot at
                    # completing its thinking phase and producing visible text.
                    # This sits before the effort-downgrade step because the
                    # model wasn't struggling with effort -- it was cut off.
                    log.warning(
                        "chat stream blank with reasoning (reasoning_tokens=%s) -- "
                        "retrying same model on fresh egress before downgrade",
                        seen.get("reasoning_tokens", 0),
                    )
                    try:
                        same_params = _escalate_output_budget(params, 2)
                        _resolve_effort(same_params, target, previous=original_effort)
                        same_iter, _, _, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id,
                            timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, **same_params,
                        )
                        same_seen: Dict[str, Any] = {}
                        same_outcome = stream_guard.StreamOutcome()
                        def _same_model_reopen():
                            fresh, _, _, _ = executor.execute_stream(
                                _messages_for_model(messages, target, had_images),
                                target, target_providers, proxy_pool=proxy_pool,
                                session_id=session_id,
                                timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                                catalog=catalog, **same_params,
                            )
                            return stream_idle.with_idle_timeout(
                                fresh, config.STREAM_IDLE_TIMEOUT, log)
                        for chunk in stream_guard.guarded_stream(
                            open_stream=_same_model_reopen,
                            first=stream_idle.with_idle_timeout(
                                same_iter, config.STREAM_IDLE_TIMEOUT, log),
                            outcome=same_outcome,
                            on_chunk=lambda raw: _harvest_stream_usage(raw, same_seen),
                            log=log,
                            enabled=recover,
                            hold=_hold_for_egress,
                            retry_model=lambda: None,
                        ):
                            yield chunk
                        # If the fresh-lane retry produced visible content, the
                        # model just needed a longer runway. Merge the usage
                        # and return -- no downgrade or fallback needed.
                        if same_seen.get("_visible_content_chars", 0) > 0 or same_seen.get("_tool_calls", 0) > 0:
                            seen.update(same_seen)
                            outcome.recovered = True
                            outcome.rescue_succeeded = True
                            outcome.completed = same_outcome.completed or outcome.completed
                            return
                        # Still blank on fresh lane -- fall through to
                        # downgrade / fallback below.
                        log.warning(
                            "chat stream blank on fresh lane too -- "
                            "falling through to downgrade/fallback"
                        )
                    except Exception as same_exc:
                        log.warning(
                            "chat stream same-model fresh-lane retry failed: %s "
                            "-- falling through to downgrade/fallback", same_exc,
                        )
                # Try downgrade first if high effort, else fallback model — per user request
                # the response pattern/translation is the culprit, not effort, so any
                # Responses-only empty at any effort should be retried, silently, without notice.
                downgrade_map = {"xhigh": "high", "max": "xhigh", "ultra": "max", "high": "medium", "medium": "low", "low": "minimal"}
                downgraded = downgrade_map.get(eff.lower()) if eff else None
                if downgraded:
                    log.warning("chat stream blank detected target=%s effort=%s visible=0 — silent retry downgraded %s -> %s (empty never forwarded)", target, eff, downgraded, target)
                    try:
                        retry_params = dict(params)
                        retry_params["reasoning_effort"] = downgraded
                        _resolve_effort(retry_params, target)
                        retry_iter, retry_prov, retry_key, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **retry_params
                        )
                        def _downgrade_reopen():
                            """Fresh stream for mid-flight retry after a
                            downgrade-attempt break. Must NOT reuse
                            ``retry_iter`` -- that generator is already being
                            consumed by ``first``, so re-entering it raises
                            "generator already executing" and the retry is
                            wasted. Going back through ``execute_stream``
                            picks a fresh exit IP under normal pool policy,
                            same as the non-downgrade ``_reopen`` path."""
                            fresh, _, _, _ = executor.execute_stream(
                                _messages_for_model(messages, target, had_images),
                                target, target_providers, proxy_pool=proxy_pool,
                                session_id=session_id,
                                timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                                catalog=catalog, **retry_params
                            )
                            return stream_idle.with_idle_timeout(
                                fresh, config.STREAM_IDLE_TIMEOUT, log)
                        retry_seen: Dict[str, Any] = {}
                        retry_outcome = stream_guard.StreamOutcome()
                        for chunk in stream_guard.guarded_stream(
                            open_stream=_downgrade_reopen,
                            first=stream_idle.with_idle_timeout(retry_iter, config.STREAM_IDLE_TIMEOUT, log),
                            outcome=retry_outcome,
                            on_chunk=lambda raw: _harvest_stream_usage(raw, retry_seen),
                            log=log,
                            enabled=recover,
                            hold=_hold_for_egress,
                            retry_model=lambda: None,
                        ):
                            yield chunk
                        seen.update(retry_seen)
                        outcome.recovered = outcome.recovered or retry_outcome.recovered
                        outcome.error = retry_outcome.error or outcome.error
                        outcome.completed = retry_outcome.completed or outcome.completed
                        return
                    except Exception as retry_exc:
                        log.warning("chat stream empty silent retry downgraded failed: %s — trying fallback model", retry_exc)
                # No downgrade or downgrade failed — fallback to a different free model
                fallback = dispatcher.fallback_model(catalog, had_images, exclude={target}, messages=messages)
                fb_provs = catalog.providers_for(fallback) if fallback else None
                if fallback and fb_provs:
                    log.warning("chat stream blank — fallback %s -> %s", target, fallback)
                    try:
                        retry_params = dict(params)
                        _resolve_effort(retry_params, fallback, previous=original_effort)
                        retry_iter2, _, _, _ = executor.execute_stream(
                            _messages_for_model(messages, fallback, had_images),
                            fallback, fb_provs, proxy_pool=proxy_pool,
                            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **retry_params
                        )
                        def _fallback_reopen():
                            """Fresh stream for mid-flight retry after a
                            fallback-model break. Must NOT reuse
                            ``retry_iter2`` -- that generator is already
                            being consumed by ``first``, so re-entering it
                            raises "generator already executing" and the
                            retry is wasted. Going back through
                            ``execute_stream`` picks a fresh exit IP under
                            normal pool policy."""
                            fresh2, _, _, _ = executor.execute_stream(
                                _messages_for_model(messages, fallback, had_images),
                                fallback, fb_provs, proxy_pool=proxy_pool,
                                session_id=session_id,
                                timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                                catalog=catalog, **retry_params
                            )
                            return stream_idle.with_idle_timeout(
                                fresh2, config.STREAM_IDLE_TIMEOUT, log)
                        for chunk in stream_guard.guarded_stream(
                            open_stream=_fallback_reopen,
                            first=stream_idle.with_idle_timeout(retry_iter2, config.STREAM_IDLE_TIMEOUT, log),
                            outcome=stream_guard.StreamOutcome(),
                            on_chunk=lambda raw: _harvest_stream_usage(raw, seen),
                            log=log,
                            enabled=recover,
                            hold=_hold_for_egress,
                            retry_model=lambda: None,
                        ):
                            yield chunk
                        return
                    except Exception as fb_exc:
                        log.warning("chat stream fallback also failed: %s", fb_exc)
            # Not retrying or retry failed — flush the original (may be empty, but now rare)
            yield from buf
        finally:
            if outcome.error:
                status = "stream_broken"
            elif outcome.recovered:
                status = "ok_recovered"
            else:
                status = None  # keep ok_stream
            # Total duration, not time-to-first-token: this column answers
            # "how long did that request take".
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=status,
                error=outcome.error,
                # A mid-flight reroute means the answer came from a different
                # model than the row was opened with. Recording the original
                # would make the ledger name a model that produced nothing.
                routed_model=reroute["model"] if reroute["model"] != target else None,
                routed_by=reroute["by"] if reroute["model"] != target else None,
                reason=reroute["reason"] if reroute["model"] != target else None,
            )
            if outcome.error:
                log.warning(
                    "chat stream rescue failed target=%s attempts=%d error=%s",
                    reroute["model"], outcome.attempts, outcome.error,
                )
            elif outcome.rescue_succeeded:
                log.info(
                    "chat stream recovered target=%s attempts=%d",
                    reroute["model"], outcome.attempts,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
            "X-Lingling-Stream-Mode": "guarded" if recover else "passthrough",
        },
    )


@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses-compatible entrypoint for Codex.

    Codex 0.144+ removed ``wire_api = "chat"`` and now insists on
    ``/v1/responses``. Lingling still talks to providers through chat
    completions, so this endpoint translates once at the API edge and reuses the
    same dispatcher, executor, proxy pool, and usage ledger as `/v1/chat/completions`.
    """
    request_started = time.time()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        requested, messages, params = responses_bridge.request_to_chat(body)
    except JSONDecodeError as exc:
        log.warning("responses invalid_json error=%s", exc)
        raise HTTPException(400, "Invalid JSON request body.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    stream = bool(body.get("stream"))
    session_id = request.headers.get("session-id") or body.get("prompt_cache_key", "")
    if not isinstance(session_id, str):
        session_id = ""
    had_images = vision_bridge.messages_have_images(messages)

    if requested == config.MULTIMODEL_ID:
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages, had_images, session_id
        )
    else:
        target = requested
        if not catalog.providers_for(target):
            bare = target.split("/", 1)[1] if "/" in target else target
            if bare != target and catalog.providers_for(bare):
                target = bare
            else:
                raise HTTPException(400, f"Unknown or unsupported model: {requested!r}")
        reason = "user requested"
        routed_by = "user"
        if catalog.is_burned(target) or catalog.is_blacklisted(target):
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            if not (fallback and fallback != target and catalog.providers_for(fallback)):
                raise HTTPException(
                    503, f"explicit pick {requested!r} is unavailable, no live substitute",
                )
            log.info("reroute %s -> %s (explicit pick unavailable)", target, fallback)
            target = fallback
            reason = f"explicit pick {requested!r} was burned; rerouted by recycler"
            routed_by = "recycler-reroute"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(503, f"No provider available for model '{target}'.")
    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)
    original_effort = params.get("reasoning_effort")
    _resolve_effort(params, target)
    call_type = "responses"

    if not stream:
        started = time.time()
        # The lane that served the first attempt. A blank turn is the model's
        # own think-phase boundary, not an egress failure, so the resume retry
        # pins back onto this same lane instead of rotating (rotation is what
        # burned lanes and queued regenerations in the health daemon).
        attempt_proxy_ref: Dict[str, Any] = {}
        try:
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, catalog=catalog,
                proxy_ref=attempt_proxy_ref, **params
            )
        except executor.AllFailedError as exc:
            # Fall back to another model, exactly as the chat path does. Without
            # this a rate-limited free tier returned a hard 503 to Codex while
            # Cline silently got an answer from a different model.
            # Model-class burning is owned by ``executor``, not this catch, so
            # we do not double-count here.
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                    call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                resp, prov, key, attempts2 = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages, fallback, had_images),
                    fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, catalog=catalog, **retry_params
                )
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images,
                    error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                    call_type=call_type,
                    attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
            attempts = exc.attempts + attempts2
            target = fallback
            reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
            routed_by = "fallback"
        usage = extract_usage(resp)
        account_id = key.id if key is not None else None
        row_id = usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=(time.time() - started) * 1000.0, status="ok",
            had_images=had_images, account_id=account_id, provider=prov.id,
            call_type=call_type, attempts=len(attempts),
            last_upstream_status=None, rerouted=(routed_by == "fallback"),
        )
        out = responses_bridge.response_object(resp, requested, target, prov.id)
        out["lingling"].update({
            "requested_model": requested, "routed_by": routed_by,
            "reason": reason, "account": account_id, "attempts": attempts,
        })
        # Muse Spark's upstream intermittently completes a turn with no output
        # items at all (see the blank-retry helper above). The old fix retried
        # with downgraded effort or a fallback model and, when both failed,
        # returned the empty response anyway -- Codex rendered that as an empty
        # assistant turn. Re-run the same request on fresh egress instead; only
        # if every attempt comes back blank do we surface an honest notice.
        if _responses_output_blank(out):
            retried = await _responses_blank_retry(
                messages, target, target_providers, params,
                session_id=session_id, had_images=had_images,
                requested=requested, original_effort=original_effort,
                attempt_proxy_ref=attempt_proxy_ref,
            )
            if retried is not None:
                out = retried
                out["lingling"].update({
                    "requested_model": requested, "routed_by": routed_by,
                    "reason": reason, "account": account_id, "attempts": attempts,
                })
                # The ledger row was opened with the blank first attempt's
                # zero usage; rewrite it with the retry's real numbers so the
                # 0/0/0 rows that made this bug visible stop being recorded.
                ru = out.get("usage") or {}
                rdet = ru.get("output_tokens_details") or {}
                usage_store.finalize(
                    row_id,
                    tokens_in=int(ru.get("input_tokens", 0) or 0),
                    tokens_out=int(ru.get("output_tokens", 0) or 0),
                    reasoning_tokens=int(rdet.get("reasoning_tokens", 0) or 0),
                )
            else:
                notice = _responses_blank_notice(target, _RESPONSES_BLANK_MAX_ATTEMPTS)
                out["output"] = [{
                    "id": f"msg_{int(time.time() * 1000)}",
                    "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": notice, "annotations": []}],
                }]
                usage_store.finalize(
                    row_id, error=f"blank after {_RESPONSES_BLANK_MAX_ATTEMPTS} attempts",
                )
                log.warning(
                    "responses blank after %d attempts target=%s -- returning notice",
                    _RESPONSES_BLANK_MAX_ATTEMPTS, target,
                )
        log.info(
            "%s  ----  %dms",
            target, int((time.time() - request_started) * 1000.0),
        )
        return JSONResponse(out)

    original_stream_target = target
    original_stream_reason = reason
    original_stream_by = routed_by
    started = time.time()
    # The lane that served the first attempt. A blank turn is the model's own
    # think-phase boundary, not an egress failure, so the resume retry pins
    # back onto this same lane instead of rotating (rotation is what burned
    # lanes and queued regenerations in the health daemon).
    attempt_proxy_ref: Dict[str, Any] = {}
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog,
            proxy_ref=attempt_proxy_ref, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        fallback = dispatcher.fallback_model(
            catalog, had_images, exclude={target}, messages=messages,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if fallback and fallback != target and fallback_providers:
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                retry_msgs = _messages_for_model(messages, fallback, had_images)
                stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                    executor.execute_stream,
                    retry_msgs, fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **retry_params
                )
                target = fallback
                target_providers = fallback_providers
                reason = f"primary stream failed ({exc.last_error}); fell back to {fallback}"
                routed_by = "fallback"
                attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                error = str(getattr(fallback_exc, "last_error", fallback_exc))
                usage_store.log(
                    requested, original_stream_target, original_stream_by, original_stream_reason,
                    status="exhausted", had_images=had_images, error=error[:300],
                    call_type=call_type,
                    attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")
        else:
            error = str(getattr(exc, "last_error", exc))
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
                call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                rerouted=(routed_by == "fallback"),
            )
            raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True, call_type=call_type, attempts=len(attempts),
        last_upstream_status=None, rerouted=(routed_by == "fallback"),
    )
    log.info(
        "%s  ----  firstchunk %dms",
        target, int((time.time() - request_started) * 1000.0),
    )

    auto_routed = requested == config.MULTIMODEL_ID
    reroute = {"model": target, "reason": reason, "by": routed_by}

    def _reopen():
        retry_model = target
        retry_params = params
        if auto_routed:
            alternative = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages,
            )
            alt_providers = catalog.providers_for(alternative)
            if alternative and alternative != target and alt_providers:
                retry_model = alternative
                retry_params = dict(params)
                _resolve_effort(retry_params, retry_model, previous=original_effort)
                reroute.update({
                    "model": retry_model,
                    "reason": f"stream broke on {target}; rerouted to {retry_model}",
                    "by": "reroute",
                })
                log.warning("responses stream rerouting mid-flight: %s -> %s", target, retry_model)
        providers_for_retry = catalog.providers_for(retry_model) or target_providers
        retry_messages = _messages_for_model(messages, retry_model, had_images)
        again, _, _, _ = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
            catalog=catalog, **retry_params
        )
        return stream_idle.with_idle_timeout(again, config.STREAM_IDLE_TIMEOUT, log)

    def _hold_for_egress():
        yield from parking.hold_stream_for_egress(proxy_pool, config.EGRESS_WAIT_BUDGET, log)

    def event_stream():
        # See /v1/chat/completions same comment: clients (Codex CLI in particular)
        # drop a connection that's silent across STREAM_FIRST_TOKEN_TIMEOUT while
        # Lingling is rolling through proxies. SSE comment lines are transparent
        # to the Responses translator but keep the socket warm.
        yield b": lingling heartbeat -- streaming start for target=" + target.encode() + b"\n\n"
        seen: Dict[str, int] = {}
        outcome = stream_guard.StreamOutcome()
        try:
            # Muse Spark's upstream intermittently completes a stream with no
            # content deltas and no usage (the ledger's 0/0/0 rows). The old
            # fix buffered the entire first stream to detect that, so the
            # client stared at silence for the whole attempt (up to 67s), then
            # silently retried with downgraded effort or a fallback model and
            # could still flush the empty buffer. Instead: stream the first
            # attempt live, and only when it lands blank re-run the request on
            # fresh egress (bounded, so a bad stretch cannot loop forever).
            # The first non-blank attempt is streamed to the client as it
            # arrives; if every attempt comes back blank, the honest notice is
            # the answer.
            pending_iter = stream_iter
            for attempt in range(1, _RESPONSES_BLANK_MAX_ATTEMPTS + 1):
                attempt_seen: Dict[str, int] = {}
                attempt_outcome = stream_guard.StreamOutcome()
                if pending_iter is None:
                    try:
                        # A blank with no continuation state is a budget-clipped
                        # think phase: re-sending the same cap re-blanks, so
                        # escalate the output budget on each fresh attempt.
                        retry_params = _escalate_output_budget(params, attempt)
                        pending_iter, _prov, _key, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, **retry_params
                        )
                    except (executor.AllFailedError, executor.NoProviderError) as exc:
                        log.warning(
                            "responses stream blank retry attempt %d/%d failed to start: %s",
                            attempt, _RESPONSES_BLANK_MAX_ATTEMPTS,
                            getattr(exc, "last_error", exc),
                        )
                        break
                attempt_iter = pending_iter
                pending_iter = None
                guarded = stream_guard.guarded_stream(
                    open_stream=_reopen,
                    first=stream_idle.with_idle_timeout(
                        attempt_iter, config.STREAM_IDLE_TIMEOUT, log),
                    outcome=attempt_outcome,
                    on_chunk=lambda raw: _harvest_stream_usage(
                        raw if isinstance(raw, bytes) else raw.encode("utf-8"),
                        attempt_seen,
                    ),
                    log=log,
                    enabled=bool(body.get("lingling_recover", config.STREAM_RECOVERY)),
                    hold=_hold_for_egress,
                    retry_model=lambda: (
                        reroute["model"] if reroute["model"] != target else None
                    ),
                )
                # The bridge's events are the client's view of the turn; the
                # attempt is blank when no visible text and no tool call ever
                # reached it. Reasoning-only turns are blank too -- Codex
                # renders them as an empty assistant message.
                buf: list[bytes] = []
                for ev in responses_bridge.stream_events(guarded, requested, attempt_outcome):
                    buf.append(ev)
                if attempt_seen.get("_visible_content_chars", 0) > 0 or attempt_seen.get("_tool_calls", 0) > 0:
                    yield from buf
                    seen.update(attempt_seen)
                    outcome.completed = attempt_outcome.completed
                    outcome.recovered = attempt_outcome.recovered
                    outcome.rescue_succeeded = attempt_outcome.rescue_succeeded
                    outcome.error = attempt_outcome.error
                    outcome.attempts = attempt_outcome.attempts
                    outcome.text_chars = attempt_outcome.text_chars
                    return
                resume_items = attempt_seen.get("_resume_items") or []
                if resume_items:
                    # The model's own think-phase boundary, not an upstream
                    # failure: it completed a reasoning item (encrypted
                    # continuation state) and ended the turn without visible
                    # text. It will think again on its own -- resume that
                    # thinking by feeding the completed reasoning item back as
                    # input. Pin to the same egress lane that just served it
                    # ONLY when the attempt ended cleanly; a stream that broke
                    # (504, connection refused, idle stall) makes the lane
                    # suspect, and pinning a dead lane wastes the attempt.
                    log.info(
                        "responses stream think-phase boundary attempt %d/%d "
                        "target=%s effort=%s -- model ended its turn after "
                        "reasoning with no visible text; resuming its thinking "
                        "via encrypted reasoning on the same lane",
                        attempt, _RESPONSES_BLANK_MAX_ATTEMPTS, target,
                        params.get("reasoning_effort") or original_effort or "",
                    )
                    try:
                        resume_params = dict(params)
                        resume_params["resume_items"] = resume_items
                        pending_iter, _prov, _key, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, pin_proxy_id=(
                                attempt_proxy_ref.get("id")
                                if not attempt_outcome.error and not attempt_outcome.recovered
                                else None
                            ),
                            **resume_params
                        )
                        continue
                    except (executor.AllFailedError, executor.NoProviderError) as exc:
                        log.warning(
                            "responses stream resume attempt %d/%d failed to start: %s",
                            attempt, _RESPONSES_BLANK_MAX_ATTEMPTS,
                            getattr(exc, "last_error", exc),
                        )
                        # The lane is dead, not the model -- the next attempt
                        # goes out on fresh egress instead of giving up.
                        continue
                if attempt_proxy_ref.get("id") and not attempt_outcome.error and not attempt_outcome.recovered:
                    # No continuation state to resume (the upstream cut the
                    # turn off mid-think without finalizing a reasoning item).
                    # The model will think again on its own -- re-run the same
                    # request on the same lane rather than rotating: the lane
                    # is not the problem, and rotation is what burned lanes
                    # and queued regenerations in the health daemon.
                    log.info(
                        "responses stream think-phase boundary attempt %d/%d "
                        "target=%s effort=%s -- model ended its turn mid-think "
                        "with no visible text and no continuation state; "
                        "re-running the same request on the same lane",
                        attempt, _RESPONSES_BLANK_MAX_ATTEMPTS, target,
                        params.get("reasoning_effort") or original_effort or "",
                    )
                    try:
                        # Re-sending the same request with the same cap
                        # re-blanks identically (budget-clipped think phase) --
                        # escalate the output budget so the think gets room.
                        retry_params = _escalate_output_budget(params, attempt + 1)
                        pending_iter, _prov, _key, _ = executor.execute_stream(
                            _messages_for_model(messages, target, had_images),
                            target, target_providers, proxy_pool=proxy_pool,
                            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
                            catalog=catalog, pin_proxy_id=attempt_proxy_ref.get("id"),
                            **retry_params
                        )
                        continue
                    except (executor.AllFailedError, executor.NoProviderError) as exc:
                        log.warning(
                            "responses stream same-lane retry attempt %d/%d failed to start: %s",
                            attempt, _RESPONSES_BLANK_MAX_ATTEMPTS,
                            getattr(exc, "last_error", exc),
                        )
                        # The lane died, not the model -- the next attempt goes
                        # out on fresh egress instead of giving up.
                        continue
                log.warning(
                    "responses stream blank attempt %d/%d target=%s effort=%s -- "
                    "upstream completed with no content; retrying on a fresh egress",
                    attempt, _RESPONSES_BLANK_MAX_ATTEMPTS, target,
                    params.get("reasoning_effort") or original_effort or "",
                )
            # Every attempt came back blank. The honest terminal is a notice,
            # not a silent empty turn -- Codex renders an empty response as a
            # broken conversation, and a notice tells the user what happened.
            notice = _responses_blank_notice(target, _RESPONSES_BLANK_MAX_ATTEMPTS)
            yield from responses_bridge.stream_events(
                iter([b'data: {"choices":[{"index":0,"delta":{"content":'
                      + json.dumps(notice).encode("utf-8")
                      + b'},"finish_reason":"stop"}]}']),
                requested, outcome,
            )
            log.warning(
                "responses stream blank after %d attempts target=%s -- returning notice",
                _RESPONSES_BLANK_MAX_ATTEMPTS, target,
            )
        finally:
            if outcome.error:
                status = "stream_broken"
            elif outcome.recovered:
                status = "ok_recovered"
            else:
                status = None if outcome.completed else "stream_broken"
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=status,
                error=outcome.error,
                routed_model=reroute["model"] if reroute["model"] != target else None,
                routed_by=reroute["by"] if reroute["model"] != target else None,
                reason=reroute["reason"] if reroute["model"] != target else None,
            )
            if outcome.error:
                log.warning(
                    "responses stream rescue failed target=%s attempts=%d error=%s",
                    reroute["model"], outcome.attempts, outcome.error,
                )
            elif outcome.rescue_succeeded:
                log.info(
                    "responses stream recovered target=%s attempts=%d",
                    reroute["model"], outcome.attempts,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
        },
    )

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages entrypoint, for Claude Code.

    Kept separate from the Codex/Responses handler on purpose: the two wire
    formats share no shape, and folding them together is how one harness's edge
    case becomes the other's regression. Everything below the translation is the
    same machinery -- dispatcher, executor, Tor egress, parking, ledger.
    """
    request_started = time.time()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        requested, messages_in, params = messages_bridge.request_to_chat(body)
    except JSONDecodeError as exc:
        log.warning("messages invalid_json error=%s", exc)
        raise HTTPException(400, "Invalid JSON request body.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    stream = bool(body.get("stream"))
    # Reasoning only reaches the client when it asked for thinking. Anthropic
    # redacts thinking by default and Claude Code relies on that; Lingling cannot
    # redact plain reasoning text, so it drops it instead of flooding the terminal.
    show_thinking = cc_thinking.wants_thinking(body)
    # Claude Code identifies a conversation with its own header; falling back to
    # the metadata user_id keeps sticky routing working for clients that send it.
    session_id = request.headers.get("x-claude-code-session-id") or ""
    if not session_id:
        meta = body.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("user_id"), str):
            session_id = meta["user_id"]
    had_images = vision_bridge.messages_have_images(messages_in)

    # Claude Code asks for Anthropic model ids and makes background calls on its
    # haiku alias; neither exists here, so they resolve to a free model by size
    # class before routing sees them.
    target = model_map.resolve(requested, catalog)
    if target == config.MULTIMODEL_ID:
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages_in, had_images, session_id
        )
    else:
        reason = f"claude code asked for {requested}"
        routed_by = "user" if target == model_map.strip_alias(requested) else "alias"
        # Same burned-reroute the other two entrypoints use, lifted verbatim.
        # `model_map.resolve` rule-1 returns an explicit-pick by its literal id
        # whenever the catalog still knows it -- without consulting burned
        # state -- so a Claude Code request naming a now-burned model would
        # otherwise be handed straight to the executor (which would 503),
        # defeating the recycler's "remove from everywhere" intent.
        if catalog.is_burned(target) or catalog.is_blacklisted(target):
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages_in,
            )
            if not (fallback and fallback != target and catalog.providers_for(fallback)):
                raise HTTPException(
                    503, f"explicit pick {requested!r} is unavailable, no live substitute",
                )
            log.info("reroute %s -> %s (explicit pick unavailable)", target, fallback)
            target = fallback
            reason = f"explicit pick {requested!r} was burned; rerouted by recycler"
            routed_by = "recycler-reroute"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(503, f"No provider available for model '{target}'.")

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages_in = vision_bridge.strip_images_for_text_model(messages_in)

    original_effort = params.get("reasoning_effort")
    # Clamp the depth label to what this model actually publishes. Must happen
    # after routing: `low` is legal for ling and meaningless for deepseek, and
    # OpenCode answers 200 for a value it does not implement.
    _resolve_effort(params, target)
    call_type = "messages"

    if not stream:
        started = time.time()
        try:
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages_in, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, catalog=catalog, **params
            )
        except executor.AllFailedError as exc:
            # Same fallback the other two entrypoints do: answering from a
            # different free model beats handing Claude Code a hard failure,
            # which ends its turn.
            # Model-class burning is owned by ``executor`` for parity with the
            # chat/responses paths -- single source of truth avoids the
            # double-count that MAX_MODEL_FAILURES=1 would punish hardest.
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages_in,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                    call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                resp, prov, key, attempts = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages_in, fallback, had_images),
                    fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, catalog=catalog, **retry_params
                )
                target = fallback
                reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
                routed_by = "fallback"
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images,
                    error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                    call_type=call_type,
                    attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")

        account_id = key.id if key is not None else None
        usage = extract_usage(resp)
        usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=(time.time() - started) * 1000.0, status="ok",
            had_images=had_images, account_id=account_id, provider=prov.id,
            call_type=call_type, attempts=len(attempts),
            last_upstream_status=None, rerouted=(routed_by == "fallback"),
        )
        out = messages_response.response_object(
            resp, requested, target, prov.id, show_thinking=show_thinking,
        )
        log.info(
            "%s  ----  %dms",
            target, int((time.time() - request_started) * 1000.0),
        )
        return JSONResponse(out)

    started = time.time()
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages_in, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        # Parity with the chat and Responses stream paths: a 429/5xx burn across
        # all egress lanes for the primary model should not surface as a 503
        # when another free model is live. Claude Code renders that 503 as a
        # dead turn, while an answer from a different model keeps the session.
        fallback = dispatcher.fallback_model(
            catalog, had_images, exclude={target}, messages=messages_in,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if fallback and fallback != target and fallback_providers:
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                retry_msgs = _messages_for_model(messages_in, fallback, had_images)
                stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                    executor.execute_stream,
                    retry_msgs, fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, catalog=catalog, **retry_params
                )
                attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
                target = fallback
                reason = f"primary stream failed ({exc.last_error}); fell back to {fallback}"
                routed_by = "fallback"
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                error = str(getattr(fallback_exc, "last_error", fallback_exc))
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=error[:300],
                    call_type=call_type,
                    attempts=len(getattr(exc, "attempts", [])) + len(getattr(fallback_exc, "attempts", [])),
                    last_upstream_status=getattr(getattr(fallback_exc, "last_error", None), "status_code", None),
                    rerouted=(routed_by == "fallback"),
                )
                raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")
        else:
            error = str(getattr(exc, "last_error", exc))
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
                call_type=call_type, attempts=len(getattr(exc, "attempts", [])),
                last_upstream_status=getattr(getattr(exc, "last_error", None), "status_code", None),
                rerouted=(routed_by == "fallback"),
            )
            raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True, call_type=call_type, attempts=len(attempts),
        last_upstream_status=None, rerouted=(routed_by == "fallback"),
    )
    log.info(
        "%s  ----  firstchunk %dms",
        target, int((time.time() - request_started) * 1000.0),
    )

    # Mid-stream recovery may land on a different model, but only when the
    # client asked the router to choose (see the chat path's identical logic).
    auto_routed = requested == config.MULTIMODEL_ID
    reroute = {"model": target, "reason": reason, "by": routed_by}

    def _reopen():
        """Open a replacement upstream stream for mid-flight recovery.

        Goes back through the executor, so the retry picks a fresh exit IP under
        the pool's normal policy rather than reusing the one that just stalled.
        For an auto-routed turn it also picks a fresh *model*, excluding the one
        that broke. Only the generator is kept; the chosen model is recorded on
        `reroute` so the ledger and the log can report where the answer came
        from.
        """
        retry_model = target
        retry_params = params
        if auto_routed:
            alternative = dispatcher.fallback_model(
                catalog, had_images, exclude={target}, messages=messages_in,
            )
            alt_providers = catalog.providers_for(alternative)
            if alternative and alternative != target and alt_providers:
                retry_model = alternative
                retry_params = dict(params)
                _resolve_effort(retry_params, retry_model, previous=original_effort)
                reroute.update({
                    "model": retry_model,
                    "reason": f"stream broke on {target}; rerouted to {retry_model}",
                    "by": "reroute",
                })
                log.warning(
                    "messages stream rerouting mid-flight: %s -> %s",
                    target, retry_model,
                )
        providers_for_retry = catalog.providers_for(retry_model) or target_providers
        retry_messages = _messages_for_model(messages_in, retry_model, had_images)
        again, _prov, _key, _attempts = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
            catalog=catalog, **retry_params
        )
        return stream_idle.with_idle_timeout(again, config.STREAM_IDLE_TIMEOUT, log)

    def _hold_for_egress():
        """Wait for a free exit before the mid-stream retry reopens.

        Same decision as the pre-first-token wait, but this one runs inside a
        live SSE response, so it emits keepalive comments instead of awaiting
        silently. Yields nothing when waiting cannot help.
        """
        yield from parking.hold_stream_for_egress(
            proxy_pool, config.EGRESS_WAIT_BUDGET, log,
        )

    def event_stream():
        # Same heartbeat rationale as the /v1/chat/completions path: the
        # token-by-token Anthropic Messages SSE channel has no event to emit
        # before first byte (no ``message_start`` until upstream speaks), so
        # the connection would sit silent across a slow proxy hand-off. A one-
        # shot SSE comment keeps the socket warm until the first frame.
        yield b": lingling heartbeat -- streaming start for target=" + target.encode() + b"\n\n"
        seen: Dict[str, int] = {}
        outcome = stream_guard.StreamOutcome()
        try:
            def tracked():
                # Idle watchdog first: an upstream that stops speaking without
                # closing would otherwise hold the turn open indefinitely -- one
                # measured session sat 885s with zero tokens before giving up.
                # StreamStalled is caught below and ends the turn honestly rather
                # than propagating into a broken SSE response.
                guarded = stream_idle.with_idle_timeout(
                    stream_iter, config.STREAM_IDLE_TIMEOUT, log)
                for raw in guarded:
                    frame = raw if isinstance(raw, bytes) else raw.encode("utf-8")
                    _harvest_stream_usage(frame, seen)
                    yield frame
            # Same mid-flight recovery the chat and Responses paths have: a
            # stall or transport break reopens on a fresh exit IP (and, for an
            # auto-routed turn, a fresh model) instead of ending the turn. The
            # reset marker is an SSE comment, which the Anthropic translator
            # skips, so Claude Code just sees the answer restart.
            recover = bool(body.get("lingling_recover", config.STREAM_RECOVERY))
            guarded = stream_guard.guarded_stream(
                open_stream=_reopen,
                first=tracked(),
                outcome=outcome,
                on_chunk=lambda raw: None,
                log=log,
                enabled=recover,
                hold=_hold_for_egress,
                retry_model=lambda: (
                    reroute["model"] if reroute["model"] != target else None
                ),
            )
            yield from messages_stream.stream_events(
                guarded, requested, outcome, show_thinking=show_thinking,
            )
        finally:
            if outcome.error:
                log.warning(
                    "messages stream broke target=%s provider=%s - %s",
                    target, prov.id, outcome.error,
                )
            elif outcome.rescue_succeeded:
                log.info(
                    "messages stream recovered target=%s attempts=%d",
                    reroute["model"], outcome.attempts,
                )
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=None if outcome.completed else "stream_broken",
                error=outcome.error,
                routed_model=reroute["model"] if reroute["model"] != target else None,
                routed_by=reroute["by"] if reroute["model"] != target else None,
                reason=reroute["reason"] if reroute["model"] != target else None,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
        },
    )


# ---------------------------------------------------------------------------
# Frontend (single-page app served from ../frontend, if present)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        """Serve the dashboard. The gateway is open -- no session, no key."""
        return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    """Run the gateway directly: `python app.py`.

    Host and port are overridable via LINGLING_HOST / LINGLING_PORT; the default
    binds loopback only.
    """
    import os

    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("LINGLING_HOST", "127.0.0.1"),
        port=int(os.getenv("LINGLING_PORT", "8000")),
        # The dashboard polls /api/tor + /api/proxies roughly every second. uvicorn's access log renders
        # each poll as a one-line INFO entry, which floods the terminal and
        # drowns the real signal (heals, escalations, startup events). Turn it
        # off -- request errors still surface through the uvicorn.error logger
        # at WARNING+.
        access_log=False,
        log_level=os.getenv("LINGLING_LOG_LEVEL", "info"),
    )
