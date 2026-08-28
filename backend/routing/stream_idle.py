"""Noticing a stream that has gone silent without dying.

The gap this closes
-------------------
``execute_stream`` guards the *first* chunk, and ``stream_guard`` retries a stream
that raises. Neither covers a stream that simply stops speaking while the
connection stays open, and a real session hit exactly that:

    id 10  stream_broken  in=0  out=0  think=0  885.0s

Fifteen minutes, no tokens, then filed as broken. Nothing killed it; nothing was
watching it. To the user that is indistinguishable from a hang.

Why httpx's own read timeout does not catch it
----------------------------------------------
``providers.base.stream_chat`` reads with ``resp.iter_lines()`` and drops empty
lines (``if line:``). SSE keepalives *are* empty lines, so they reset httpx's read
timeout while delivering nothing usable upstream. The clock therefore has to be
kept where the usable frames are counted, not at the socket.

How
---
The upstream generator is drained by a worker thread onto a queue, and the caller
reads that queue with a deadline. No frame within the budget raises
:class:`StreamStalled`, which the chat path feeds into its existing retry and the
messages path turns into an honest end-of-turn.

What happens to the abandoned upstream
--------------------------------------
It cannot be closed from here, and the first version of this module wrongly
claimed otherwise. A generator that another thread is currently blocked inside
raises ``ValueError: generator already executing`` on ``close()`` -- measured --
so the ``finally`` in ``stream_chat`` never runs and the httpx connection is not
released at that moment.

Instead the reader thread is left to finish on its own. It does: the provider
builds its client with an httpx timeout (``httpx.Timeout(REQUEST_TIMEOUT)`` for
non-stream, ``STREAM_READ_TIMEOUT`` for streams), so a socket that has
genuinely gone quiet raises there, the generator's ``finally`` closes the client,
and the daemon thread exits. The consumer has long since returned, so the user
never waits for it. What this module guarantees is that *the request* is not held
hostage; the connection is reclaimed on the provider's own timeout.

That makes the provider's timeout (``REQUEST_TIMEOUT`` / ``STREAM_READ_TIMEOUT``)
the upper bound on a leaked connection, which is why the idle budget is much
smaller than it.

The budget is deliberately generous. When a free model thinks it streams
``reasoning_content`` continuously -- measured at 1203 frames for one turn -- so a
long think keeps the frames flowing and only a true stall goes quiet.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Generator, Iterable


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
