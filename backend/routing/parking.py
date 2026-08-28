"""Waiting out an exhausted egress pool instead of failing the request.

When every Tor lane answers 429 at once, the request used to end as HTTP 503.
For a coding agent that is not one lost turn: Cline and Codex abandon the whole
task, and the human restarts from scratch -- while the pool quietly recovers a
minute later and sits idle.

But the exits are only *cooling*, not dead, and the pool already knows exactly
when the next one comes back (:meth:`ProxyPool.time_until_available`). So the
request waits for it and tries again. A turn that takes 70 seconds is an
annoyance; a turn that fails is a dead session, and nobody would trade those
the other way around.

Two deliberate choices:

* **The wait happens on the event loop** (``asyncio.sleep``), never inside a
  worker thread. A parked request must not hold a threadpool slot that the
  executor needs for the requests that can still go out.
* **The pool's own state is the trigger.** There is no "was this a rate limit?"
  inspection of the error, because ``mark_failure`` only cools an exit for the
  statuses where a different IP would actually help. If nothing is in cooldown
  there is nothing to wait for, and waiting cannot fix a 400.

"Saturated" lanes (carrying ``PROXY_MAX_PARALLEL_STREAMS`` in-flight streams)
are also treated as unavailable: a request that lands on a saturated lane will
get the same per-IP free-tier brownout the saturated lane's other streams are
already burning. ``seconds_to_wait`` consults the lane's cooldown alone --
the saturation check is the picker's job -- so parking here picks the right
moment to retry (a cooled lane coming back *and* an in-flight stream finishing
usually coincide) without holding on the saturated one.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Generator, Optional

# An SSE comment line. Clients ignore comments, so these keep a held connection
# warm without ever looking like a content chunk to the harness on the far end.
KEEPALIVE_FRAME = b": lingling is holding for a free exit\n\n"

# How long each slice of a mid-stream hold is. The wait is chopped up so a
# keepalive goes out roughly every second and the worker thread driving the
# stream is released between slices instead of being parked for the whole wait.
_SLICE_S = 1.0


def seconds_to_wait(proxy_pool: Any, budget_s: float, log: Any) -> float:
    """How long a retry should wait for egress capacity; 0 when it would not help.

    Returns ``0.0`` when:

    * the feature is switched off (``budget_s <= 0``);
    * there is no pool, so every request already egresses from one IP and a
      later attempt would come from that same burned IP;
    * an exit is available right now, so the failure was not about exhaustion;
    * the soonest exit returns after ``budget_s``, in which case waiting would
      strand the client for longer than the answer is worth.

    Callers treat ``0.0`` as "nothing changed, fail exactly as before".

    Saturation check: if every non-cooled lane is at
    ``PROXY_MAX_PARALLEL_STREAMS`` already, the soonest cooled lane's
    cooldown is *also* the soonest pick (a stream finishing will free up
    capacity only after the cooled exit cools). Wired through
    ``active_streams`` (per-lane live-stream count) so the executor's
    pick-time cap and parking's wait window make the same decision about
    what "soonest available" means.
    """
    if budget_s <= 0 or proxy_pool is None:
        return 0.0

    remaining: Optional[float] = proxy_pool.time_until_available()
    if remaining is None or remaining <= 0:
        return 0.0
    if remaining > budget_s:
        log.warning(
            "egress: every exit is cooling and the soonest needs %.1fs, over the %.0fs budget",
            remaining, budget_s,
        )
        return 0.0
    return remaining


async def wait_for_egress(
    proxy_pool: Any,
    budget_s: float,
    log: Any,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> float:
    """Hold a not-yet-started request until an exit frees up; return seconds waited.

    Used before the first token, where the handler is still a coroutine and no
    HTTP status has been sent -- so the wait is a plain ``await`` and the retry
    afterwards is indistinguishable from a slow first attempt.
    """
    remaining = seconds_to_wait(proxy_pool, budget_s, log)
    if not remaining:
        return 0.0

    log.warning("egress: every exit is cooling; holding the request %.1fs for the next one", remaining)
    await sleep(remaining)
    # The slept duration, not a measured one: a caller that injects its own
    # sleep (the tests do) still gets a truthful "this is what it waited for".
    return remaining


def hold_stream_for_egress(
    proxy_pool: Any,
    budget_s: float,
    log: Any,
    sleep: Callable[[float], None] = time.sleep,
) -> Generator[bytes, None, None]:
    """Hold a mid-stream retry until an exit frees up, yielding SSE keepalives.

    The same wait as :func:`wait_for_egress`, for the one place that cannot
    ``await``: recovery happens inside an already-running SSE response, driven by
    a synchronous generator. Two consequences shape this version.

    First, HTTP 200 was sent long ago and the client is watching a half-written
    answer, so silence reads as a hung connection -- hence a comment frame per
    slice. Second, sleeping in one-second slices means each ``next()`` returns
    promptly, so the worker thread driving the stream is handed back between
    slices rather than blocked for the entire wait.

    Yields complete, terminated frames; the caller forwards them untouched and
    must not feed them to usage harvesting, since no upstream produced them.
    """
    remaining = seconds_to_wait(proxy_pool, budget_s, log)
    if not remaining:
        return

    log.warning(
        "egress: every exit is cooling; holding the broken stream %.1fs for the next one",
        remaining,
    )
    while remaining > 0:
        yield KEEPALIVE_FRAME
        slice_s = min(_SLICE_S, remaining)
        sleep(slice_s)
        remaining -= slice_s
