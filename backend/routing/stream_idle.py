"""Notice a stream that has gone silent without dying.

``execute_stream`` guards the first chunk and ``stream_guard`` retries a stream
that raises; neither covers a stream that stops producing while the connection
stays open (one real session sat 885s with zero tokens). httpx's read timeout
misses it too: the provider reads with ``iter_lines()`` and drops empty lines,
so SSE keepalives reset the read timeout while delivering nothing usable.

A worker thread drains the upstream onto a bounded queue; the caller reads with
a deadline and raises :class:`StreamStalled` if no frame arrives in budget. The
reader is then left to finish on its own -- it cannot be closed from here
(closing a generator another thread is blocked in raises ValueError), and its
own ``REQUEST_TIMEOUT`` ends the connection (which is why the idle budget is
smaller than it).
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Generator, Iterable, Tuple

from core import config


class StreamStalled(Exception):
    """No usable frame arrived within the idle budget."""

    def __init__(self, seconds: float, frames: int) -> None:
        super().__init__(
            f"upstream sent nothing for {seconds:.0f}s after {frames} frame(s)"
        )
        self.seconds = seconds
        self.frames = frames


# A sentinel distinguishable from any real frame.
_DONE = object()


def pacing_for(reasoning: bool) -> Tuple[float, float]:
    """Idle-watchdog budget and httpx read timeout for one outbound stream.

    A reasoning model that hides its thinking tokens stays silent on the wire
    while it reasons; the default watchdog and httpx read budgets would both
    fire on that silence and ``stream_guard`` would retry the model to death --
    while it was merely thinking. Reasoning models therefore get a longer
    "thinking patience": the watchdog budget is :data:`config.STREAM_THINKING_TIMEOUT`
    and the httpx read timeout sits a first-token budget above it, so the
    watchdog's :class:`StreamStalled` (informative, one retry via stream_guard)
    governs a silent pause rather than a bare httpx ``ReadTimeout``. Non-reasoning
    models keep the tight defaults -- they never legitimately go quiet, so the
    watchdog still catches a real stall and first-token failover stays fast.

    ``STREAM_THINKING_TIMEOUT <= 0`` disables the extension (reasoning models
    fall back to the default budgets); it is not the watchdog-disable sentinel,
    which is ``STREAM_IDLE_TIMEOUT <= 0``.
    """
    if reasoning and config.STREAM_THINKING_TIMEOUT > 0:
        return config.STREAM_THINKING_TIMEOUT, config.STREAM_THINKING_TIMEOUT + config.STREAM_FIRST_TOKEN_TIMEOUT
    return config.STREAM_IDLE_TIMEOUT, config.STREAM_FIRST_TOKEN_TIMEOUT


def with_idle_timeout(
    source: Iterable[bytes],
    budget_s: float,
    log: Any = None,
) -> Generator[bytes, None, None]:
    """Yield from ``source``, raising :class:`StreamStalled` if it goes quiet.

    ``budget_s <= 0`` disables the watchdog and yields straight through, so the
    old behaviour is one config change away.

    The reader runs in a daemon thread because the upstream iterator blocks in
    ``next()`` and cannot be polled. See the module docstring for what happens to
    that thread and its connection after a stall -- in short, it is not closed
    here, and it is not left forever either.
    """
    if budget_s <= 0:
        yield from source
        return

    # Bounded so a fast upstream cannot buffer an entire response in memory while
    # the consumer is slow; the reader blocks on `put` instead.
    frames: queue.Queue = queue.Queue(maxsize=64)
    # Set when the consumer gives up, so a reader blocked on a full queue notices
    # and stops rather than filling memory for a request nobody is reading.
    abandoned = threading.Event()

    def pump() -> None:
        try:
            for item in source:
                while not abandoned.is_set():
                    try:
                        frames.put(item, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if abandoned.is_set():
                    return
        except BaseException as exc:                     # noqa: BLE001
            # Forwarded verbatim so the caller's existing error handling keeps
            # working; wrapping it would hide the upstream status code.
            if not abandoned.is_set():
                frames.put(exc)
            return
        if not abandoned.is_set():
            frames.put(_DONE)

    reader = threading.Thread(target=pump, name="lingling-stream-reader", daemon=True)
    reader.start()

    seen = 0
    try:
        while True:
            try:
                item = frames.get(timeout=budget_s)
            except queue.Empty:
                if log is not None:
                    log.warning(
                        "stream: upstream silent for %.0fs after %d frame(s) - "
                        "treating as a broken stream", budget_s, seen,
                    )
                raise StreamStalled(budget_s, seen)
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            seen += 1
            yield item
    finally:
        # Tells the reader to stop as soon as its own read returns or times out.
        # Deliberately *not* followed by source.close(): the reader is inside the
        # generator, and closing it from here raises ValueError and releases
        # nothing. The provider's own request timeout ends it.
        abandoned.set()
