"""Notice a stream that has gone silent without dying.

``execute_stream`` guards the first chunk and ``stream_guard`` retries a stream
that raises; neither covers a stream that simply stops producing while the
connection stays open. One real session sat 885 seconds with zero tokens before
being filed as broken.

httpx's read timeout does not catch it either: the provider reads with
``iter_lines()`` and drops empty lines, so SSE keepalives (which are empty
lines) reset the read timeout while delivering nothing usable. The clock has to
tick on usable frames.

The upstream generator is drained onto a bounded queue by a worker thread, and
the caller reads that queue with a deadline; no frame within the budget raises
:class:`StreamStalled`. The reader thread is then left to finish on its own --
it cannot be closed from here (closing a generator another thread is blocked
inside raises ``ValueError``), and its own ``REQUEST_TIMEOUT`` ends the
connection. That timeout is why the idle budget is much smaller than it.
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
