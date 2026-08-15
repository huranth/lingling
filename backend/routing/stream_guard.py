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
from json import JSONDecodeError
from typing import Any, Callable, Dict, Generator, Optional

# A frame carrying this key tells a client to discard everything rendered so far.
RESET_KEY = "lingling_reset"


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
    """
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    for ch in choices:
        if isinstance(ch, dict) and ch.get("finish_reason"):
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
                on_chunk(raw)
                yield raw + b"\n\n"
        except Exception as exc:  # noqa: BLE001 - any transport failure is a break
            broke = exc

        if outcome.completed:
            # The model finished. A disconnect after this point is harmless.
            if broke is not None:
                log.info(
                    "stream: upstream dropped after completion (%s) - not retrying", broke
                )
            return

        reason = str(broke) if broke is not None else "upstream closed before completing"
        if not enabled or attempt >= 2:
            outcome.error = reason
            log.warning(
                "stream: unrecoverable after %d attempt(s) - %s", attempt, reason
            )
            return

        # Retry once, on a fresh stream and therefore a fresh exit IP.
        log.warning("stream: broke mid-flight (%s) - retrying once", reason)
        # When every exit is cooling, reopening now would spend the single retry
        # on an exit that must refuse. Wait for one first, keeping the client's
        # connection alive while we do.
        if hold is not None:
            yield from hold()
        try:
            source = open_stream()
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{reason}; retry could not start: {exc}"
            log.warning("stream: retry could not start - %s", exc)
            return

        attempt += 1
        outcome.attempts = attempt
        outcome.recovered = True
        outcome.text_chars = 0
        # Tell the client to discard the partial answer before the new one lands.
        moved = retry_model() if retry_model is not None else None
        yield reset_frame(reason, attempt, moved) + b"\n\n"

