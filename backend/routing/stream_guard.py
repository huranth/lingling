"""One mid-stream retry for the streaming chat path.

Pre-first-chunk failover belongs to the executor. This covers the other failure:
a stream that dies after bytes are on the wire, when HTTP can no longer change
its status. It wraps the upstream generator, keeps a copy of the text, and if
the stream ends before the model reports completion it reopens once (a fresh
exit IP) and emits the replacement after a reset marker: ``{"lingling_reset"}``.

The retried answer differs from the partial one (models aren't deterministic),
so recovery is opt-out per request (``{"lingling_recover": false}``) and via
``LINGLING_STREAM_RECOVERY=0``. One retry, not a loop; a stream that hangs
without dying is not covered. One response's text is held in memory meanwhile.
"""

from __future__ import annotations

import json
import time
from json import JSONDecodeError
from typing import Any, Callable, Dict, Generator, Optional

from routing import pacing_memory, stream_idle

# A frame carrying this key tells a client to discard everything rendered so far.
RESET_KEY = "lingling_reset"

# Wall-clock between "still streaming" heartbeats on a live stream. Complements
# the silence watchdog (which catches a hidden-reasoning model that never
# speaks) by confirming a stream that keeps emitting frames across many seconds
# is alive, not stuck -- an operator tailing the log can tell a long reasoning
# token from a hung connection without crossing into the ledger. A stream that
# completes inside the interval streams quietly.
_HEARTBEAT_INTERVAL_S = 30.0


def _parse_sse(raw: bytes) -> Optional[Dict[str, Any]]:
    """Decode one ``data:`` line into a dict, or None if it is not one."""
    if not raw.startswith(b"data:"):
        return None
    payload = raw[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def chunk_is_terminal(obj: Dict[str, Any]) -> bool:
    """True when a chunk reports the model finished on its own terms.

    ``finish_reason`` is set on the last content chunk (``stop``, ``length``,
    ``tool_calls``, ...). Its presence is the signal that a later disconnect is
    harmless: the answer was already complete, so retrying would only burn a
    request and rewrite text the user has correctly received.

    OpenCode's free stream (and several other OpenAI-compatible gateways) never
    sets ``finish_reason`` and never sends ``[DONE]``: the response ends with a
    frame carrying ``usage`` (and a trailing ``cost``) and then a clean close.
    Without recognizing that frame here, every such *completed* stream is
    misread as a mid-flight break and retried once -- delivering the answer to
    the client twice (separated by a reset marker), doubling request/egress
    spend, and logging "broke mid-flight / unrecoverable" on every reply. An
    empty-``choices`` cushion frame by itself is NOT terminal (OpenCode sends
    several as a preamble before any content); the terminal signal is the
    end-of-stream usage/cost frame.
    """
    choices = obj.get("choices")
    if isinstance(choices, list):
        for ch in choices:
            if isinstance(ch, dict) and ch.get("finish_reason"):
                return True
    # The final usage report (OpenAI streaming's `choices:[]`+`usage` frame, or
    # OpenCode's same frame without finish_reason) marks the completion the
    # ``finish_reason`` path would have caught; the trailing ``cost`` frame is
    # OpenCode-specific metadata that always follows it.
    if isinstance(obj.get("usage"), dict):
        return True
    if "cost" in obj:
        return True
    return False


def chunk_text_len(obj: Dict[str, Any]) -> int:
    """Length of visible content in a chunk, used to tell "empty" from "partial".

    Reasoning text is excluded deliberately: a stream that emitted only thinking
    tokens before dying has produced nothing the user can use, and is worth
    retrying even though bytes were sent.
    """
    total = 0
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return 0
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        for holder_key in ("delta", "message"):
            holder = ch.get(holder_key)
            if isinstance(holder, dict) and isinstance(holder.get("content"), str):
                total += len(holder["content"])
    return total


def chunk_reasons(obj: Dict[str, Any]) -> bool:
    """A streamed chunk that carries reasoning/thinking tokens.

    OpenAI streaming surfaces thinking as ``delta.reasoning_content``; some
    gateways nest it under ``reasoning`` or ``thinking`` (a string or a non-empty
    dict). Its presence is definitive: the model reasons. The catalog flag and a
    client's reasoning param can both miss a model whose reasoning is hidden, so
    observing it on the wire is the way a hidden-reasoning model gets learned
    (see :mod:`routing.pacing_memory`) -- without that, a future model of the
    same kind would stall on every turn until an operator edits the override.

    Only a truthy value counts: a bare ``"reasoning": false`` or empty dict is
    not reasoning-in-progress.
    """
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        holder = ch.get("delta") if isinstance(ch.get("delta"), dict) else ch.get("message")
        if not isinstance(holder, dict):
            continue
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = holder.get(key)
            if (isinstance(val, str) and val) or (isinstance(val, dict) and val):
                return True
    return False


def reset_frame(reason: str, attempt: int, model: Optional[str] = None) -> bytes:
    """Build the SSE frame that tells a client to start its rendering over.

    ``model`` names the model the retry is going out on. It is only present when
    the retry actually moved -- an auto-routed turn re-decides rather than
    reopening on the model that just stalled -- so a client can say *why* the
    answer is about to change instead of always claiming a new exit IP.
    """
    payload: Dict[str, Any] = {"reason": reason[:200], "attempt": attempt}
    if model:
        payload["model"] = model
    return b"data: " + json.dumps({
        RESET_KEY: payload,
        # `choices: []` keeps the frame shaped like a normal chunk, so a strict
        # client parsing choices[0] skips it rather than erroring.
        "choices": [],
    }).encode("utf-8")


class StreamOutcome:
    """Mutable record of how a guarded stream ended, read after it is consumed."""

    def __init__(self) -> None:
        self.attempts = 1
        self.recovered = False
        self.completed = False
        self.error: Optional[str] = None
        self.text_chars = 0


def guarded_stream(
    open_stream: Callable[[], Generator[bytes, None, None]],
    first: Generator[bytes, None, None],
    outcome: StreamOutcome,
    on_chunk: Callable[[bytes], None],
    log,
    enabled: bool = True,
    hold: Optional[Callable[[], Generator[bytes, None, None]]] = None,
    retry_model: Optional[Callable[[], Optional[str]]] = None,
    model_id: Optional[str] = None,
) -> Generator[bytes, None, None]:
    """Yield an upstream stream, retrying once if it dies before completing.

    ``open_stream`` must open a *new* upstream stream when called -- it goes
    through the executor again, so the retry lands on a different exit IP under
    the pool's normal policy. ``first`` is the already-open generator (the caller
    has consumed its first chunk to prove the connection works). ``on_chunk``
    sees every raw frame that is forwarded, which is how usage harvesting stays
    wired up.

    ``hold`` runs immediately before the retry and may yield frames of its own
    while it waits for egress capacity (see :func:`routing.parking.hold_stream_for_egress`).
    Without it the retry reopens instantly and, when every exit is in cooldown,
    burns its one attempt on an exit that is guaranteed to refuse. Its frames go
    straight to the client and are deliberately *not* passed to ``on_chunk``:
    they carry no upstream usage to harvest.

    ``retry_model`` is consulted after ``open_stream`` has run and reports which
    model the retry actually went out on, or None when it did not move. It is
    mirrored into the reset frame so a client can distinguish "same model, fresh
    exit IP" from "different model" -- the dashboard says one or the other, and
    saying the wrong one is worse than saying nothing.

    The generator never raises: a failed recovery ends the stream quietly, with
    the reason recorded on ``outcome`` for the caller to log.
    """
    source = first
    attempt = 1
    # Heartbeat cadence: log "still streaming" every _HEARTBEAT_INTERVAL_S of
    # wall-clock while frames flow, so a long reasoning stream is visibly alive
    # rather than indistinguishable from a hung connection in the log. elapsed
    # is measured from stream start (kept across the single retry); frames reset
    # per attempt so the count names the attempt currently on the wire.
    started_at = time.monotonic()
    heartbeat_at = started_at + _HEARTBEAT_INTERVAL_S
    frames = 0

    while True:
        broke: Optional[Exception] = None
        try:
            for line in source:
                if not line:
                    continue
                raw = line if isinstance(line, bytes) else line.encode("utf-8")
                obj = _parse_sse(raw)
                if obj is not None:
                    outcome.text_chars += chunk_text_len(obj)
                    if chunk_is_terminal(obj):
                        outcome.completed = True
                    # A chunk carrying reasoning tokens is definitive evidence the
                    # model reasons. The catalog/override/body check can all miss
                    # a model whose reasoning is hidden, so learning it here lets a
                    # future such model self-adapt after its first turn instead of
                    # stalling every time. Idempotent on the hot path (see
                    # pacing_memory.mark_reasoning), so every reasoning chunk is
                    # safe to observe.
                    if model_id and chunk_reasons(obj):
                        pacing_memory.mark_reasoning(model_id)
                on_chunk(raw)
                yield raw + b"\n\n"
                frames += 1
                now_mono = time.monotonic()
                if now_mono >= heartbeat_at:
                    log.info(
                        "stream: still streaming model=%s attempt=%d frames=%d chars=%d elapsed=%.0fs",
                        model_id, attempt, frames, outcome.text_chars,
                        now_mono - started_at,
                    )
                    heartbeat_at = now_mono + _HEARTBEAT_INTERVAL_S
        except Exception as exc:  # noqa: BLE001 - any transport failure is a break
            broke = exc

        if outcome.completed:
            # The model finished. A disconnect after this point is harmless.
            # "recovered" means the retry actually produced a complete answer,
            # not merely that a retry was attempted -- so it is set here (only
            # when a retry happened, attempt > 1) rather than optimistically at
            # retry start. The earlier optimistic write logged "recovered" and
            # flagged ok_recovered even when the retry also broke.
            if attempt > 1:
                outcome.recovered = True
            if broke is not None:
                log.info(
                    "stream: upstream dropped after completion (%s) - not retrying", broke
                )
            return

        reason = str(broke) if broke is not None else "upstream closed before completing"
        # A stream that went silent before emitting any visible content is the
        # signature of a hidden-reasoning model thinking through its first token:
        # the watchdog saw bytes stop for the whole budget without the model ever
        # speaking, which a model that displays its reasoning never does. Learn it
        # now -- before the retry re-derives pacing, so the retry gets the thinking
        # patience and can wait the silence out instead of stalling a second time.
        # (guarded_stream is the chat path; the messages/responses entrypoints have
        # no mid-flight retry, but the learned entry still helps their next request
        # via _stream_pacing.) Gated on text_chars==0 so a model that genuinely
        # stalls mid-content -- it already spoke, so it is not hidden-thinking --
        # is not mis-learned and does not get its first-token failover loosened.
        if model_id and isinstance(broke, stream_idle.StreamStalled) and outcome.text_chars == 0:
            pacing_memory.mark_reasoning(model_id)
        if not enabled or attempt >= 2:
            outcome.error = reason
            log.warning(
                "stream: gave up after %d attempt(s) — %s", attempt, reason
            )
            return

        # Retry once, on a fresh stream and therefore a fresh exit IP.
        log.warning("stream: broke mid-flight (%s) — rerolling once", reason)
        # When every exit is cooling, reopening now would spend the single retry
        # on an exit that must refuse. Wait for one first, keeping the client's
        # connection alive while we do.
        if hold is not None:
            yield from hold()
        try:
            source = open_stream()
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{reason}; retry could not start: {exc}"
            log.warning("stream: reroll could not start — %s", exc)
            return

        attempt += 1
        outcome.attempts = attempt
        outcome.text_chars = 0
        frames = 0
        heartbeat_at = time.monotonic() + _HEARTBEAT_INTERVAL_S
        # Tell the client to discard the partial answer before the new one lands.
        moved = retry_model() if retry_model is not None else None
        yield reset_frame(reason, attempt, moved) + b"\n\n"

