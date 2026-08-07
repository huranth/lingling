"""Token and request usage logging, backed by SQLite.

Every routed request is recorded: which model was requested, which model and
*provider* it was routed to, who made the routing decision (user / dispatcher /
fallback), the dispatcher's reason, input/output token counts, latency, and
outcome. ``/api/usage`` aggregates this into totals, a per-model breakdown, a
per-provider breakdown, and a recent-requests feed for the dashboard.

Streaming requests are logged in two phases. :meth:`UsageStore.log` returns the
new row id at first-chunk time (so a stream that dies mid-flight still leaves a
record), then :meth:`UsageStore.finalize` fills in token counts and the true
duration once the upstream emits its terminal ``usage`` chunk. Without that
second phase every streamed request records zero tokens -- which is what made
the dashboard ledger read empty.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from core import config

# Hard ceiling on rows any single query may return. Both `recent` and `since`
# take their limit from a query string.
MAX_ROWS = 1000


class UsageStore:
    """A small, thread-safe SQLite usage ledger."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        config.ensure_data_dir()
        self._path = str(db_path or config.USAGE_DB)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              REAL    NOT NULL,
                    requested_model TEXT,
                    routed_model    TEXT,
                    provider        TEXT,
                    routed_by       TEXT,
                    reason          TEXT,
                    tokens_in       INTEGER DEFAULT 0,
                    tokens_out      INTEGER DEFAULT 0,
                    latency_ms      REAL    DEFAULT 0,
                    status          TEXT,
                    had_images      INTEGER DEFAULT 0,
                    account_id      TEXT,
                    error           TEXT
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_log_ts ON request_log(ts)")
            # Idempotent migrations for databases created before a column existed.
            for ddl in (
                "ALTER TABLE request_log ADD COLUMN provider TEXT",
                "ALTER TABLE request_log ADD COLUMN reasoning_tokens INTEGER DEFAULT 0",
                "ALTER TABLE request_log ADD COLUMN streamed INTEGER DEFAULT 0",
            ):
                try:
                    self._conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already present
            self._conn.commit()

    def log(
        self,
        requested_model: str,
        routed_model: str,
        routed_by: str,
        reason: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        status: str = "ok",
        had_images: bool = False,
        account_id: str = "",
        provider: str = "",
        error: str = "",
        reasoning_tokens: int = 0,
        streamed: bool = False,
    ) -> int:
        """Insert a request record. Returns the new row id.

        Callers that stream keep the id and pass it to :meth:`finalize` once the
        upstream reports its token usage.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO request_log (
                    ts, requested_model, routed_model, provider, routed_by, reason,
                    tokens_in, tokens_out, latency_ms, status, had_images,
                    account_id, error, reasoning_tokens, streamed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), requested_model, routed_model, provider, routed_by,
                    reason, tokens_in, tokens_out, latency_ms, status,
                    int(had_images), account_id, error, reasoning_tokens, int(streamed),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finalize(
        self,
        row_id: int,
        tokens_in: int = 0,
        tokens_out: int = 0,
        reasoning_tokens: int = 0,
        latency_ms: Optional[float] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        routed_model: Optional[str] = None,
        routed_by: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Fill in a streamed request's totals once the stream has closed.

        Only non-null arguments are written, so a stream that ends without a
        usage chunk keeps its first-chunk latency and ``ok_stream`` status
        rather than being zeroed out.

        ``routed_model``/``routed_by``/``reason`` exist because a stream can be
        rerouted mid-flight: the row is opened when the first chunk arrives, but
        an auto-routed turn whose model stalls is retried on a different one. Left
        at the opening values the ledger would credit the answer to a model that
        produced nothing.
        """
        sets = ["tokens_in = ?", "tokens_out = ?", "reasoning_tokens = ?"]
        args: List[Any] = [int(tokens_in), int(tokens_out), int(reasoning_tokens)]
        if latency_ms is not None:
            sets.append("latency_ms = ?")
            args.append(float(latency_ms))
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if error is not None:
            sets.append("error = ?")
            args.append(error[:300])
        if routed_model is not None:
            sets.append("routed_model = ?")
            args.append(routed_model)
        if routed_by is not None:
            sets.append("routed_by = ?")
            args.append(routed_by)
        if reason is not None:
            sets.append("reason = ?")
            args.append(reason[:300])
        args.append(int(row_id))
        with self._lock:
            self._conn.execute(
                f"UPDATE request_log SET {', '.join(sets)} WHERE id = ?", args
            )
            self._conn.commit()

    def reset(self) -> int:
        """Delete every request record. Returns how many rows were removed.

        Exposed so the operator can clear development/test traffic without
        hand-editing the database file.
        """
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
            self._conn.execute("DELETE FROM request_log")
            # Reclaim the ids so the next request starts from 1 again.
            try:
                self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'request_log'")
            except sqlite3.OperationalError:
                pass
            self._conn.commit()
        # VACUUM on a *separate* connection: on the shared connection it would
        # interleave with in-flight queries, and it is slow on large databases,
        # so it should not sit under the main lock either. A fresh connection
        # keeps it completely off the hot path.
        try:
            _vac = sqlite3.connect(self._path)
            try:
                _vac.execute("VACUUM")
            finally:
                _vac.close()
        except sqlite3.Error:
            pass
        return int(n)

    # Statuses that represent a delivered answer. Streamed rows land as
    # ``ok_stream`` at first chunk and stay that way; ``ok_recovered`` is a stream
    # that died mid-flight and was re-sent from a fresh exit IP, so the user did
    # get a complete answer. All three count as success -- filing `ok_recovered`
    # as failed made a working recovery look like an outage in every aggregate.
    _OK_STATUSES = ("ok", "ok_stream", "ok_recovered")
    # Rendered into the SQL below rather than parameterised: these are module
    # constants, and sqlite has no way to bind a variable-length IN list.
    _OK_SQL = "('ok','ok_stream','ok_recovered')"

    def summary(self) -> Dict[str, Any]:
        ok_sql = self._OK_SQL
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT
                    COUNT(*)                       AS requests,
                    COALESCE(SUM(tokens_in), 0)    AS tokens_in,
                    COALESCE(SUM(tokens_out), 0)   AS tokens_out,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    -- Average only over rows that actually produced a response;
                    -- failed rows log latency 0 and would drag the mean down.
                    COALESCE(AVG(CASE WHEN status IN {ok_sql} AND latency_ms > 0
                                      THEN latency_ms END), 0) AS avg_latency_ms,
                    COALESCE(MAX(CASE WHEN status IN {ok_sql} THEN latency_ms END), 0) AS max_latency_ms,
                    SUM(CASE WHEN status IN {ok_sql} THEN 1 ELSE 0 END) AS ok,
                    SUM(CASE WHEN status NOT IN {ok_sql} THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN streamed = 1 THEN 1 ELSE 0 END) AS streamed,
                    SUM(CASE WHEN had_images = 1 THEN 1 ELSE 0 END) AS image_requests,
                    COALESCE(MAX(ts), 0)           AS last_ts
                FROM request_log
                """
            ).fetchone()
            per_model = self._conn.execute(
                f"""
                SELECT routed_model,
                       COUNT(*)                     AS requests,
                       COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency_ms,
                       SUM(CASE WHEN status NOT IN {ok_sql} THEN 1 ELSE 0 END) AS failed
                FROM request_log GROUP BY routed_model ORDER BY requests DESC
                """
            ).fetchall()
            per_provider = self._conn.execute(
                """
                SELECT provider,
                       COUNT(*)                     AS requests,
                       COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out
                FROM request_log GROUP BY provider ORDER BY requests DESC
                """
            ).fetchall()
            per_router = self._conn.execute(
                """
                SELECT routed_by, COUNT(*) AS requests
                FROM request_log GROUP BY routed_by ORDER BY requests DESC
                """
            ).fetchall()
            per_status = self._conn.execute(
                """
                SELECT status, COUNT(*) AS requests
                FROM request_log GROUP BY status ORDER BY requests DESC
                """
            ).fetchall()
        requests = row["requests"] or 0
        ok = row["ok"] or 0
        return {
            "totals": {
                "requests": requests,
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "tokens_total": row["tokens_in"] + row["tokens_out"],
                "reasoning_tokens": row["reasoning_tokens"],
                "avg_latency_ms": round(row["avg_latency_ms"], 1),
                "max_latency_ms": round(row["max_latency_ms"], 1),
                "ok": ok,
                "failed": row["failed"] or 0,
                "streamed": row["streamed"] or 0,
                "success_rate": round(ok / requests * 100, 1) if requests else 0.0,
                "image_requests": row["image_requests"] or 0,
                "last_ts": row["last_ts"] or 0,
            },
            "per_model": [dict(r) for r in per_model],
            "per_provider": [dict(r) for r in per_provider],
            "per_router": [dict(r) for r in per_router],
            "per_status": [dict(r) for r in per_status],
        }

    def recent(self, limit: int = 500) -> List[Dict[str, Any]]:
        """The newest ``limit`` rows, newest first.

        Capped like :meth:`since`: ``limit`` arrives straight from a query
        string, so it must not be trusted unbounded.
        """
        limit = max(1, min(int(limit), MAX_ROWS))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, ts, requested_model, routed_model, provider, routed_by,
                       reason, tokens_in, tokens_out, reasoning_tokens, streamed,
                       latency_ms, status, had_images, account_id, error
                FROM request_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def since(self, after_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        """Rows newer than ``after_id``, oldest first.

        The dashboard polls this to append new traffic without refetching the
        whole log, which is what makes the ledger update live.
        """
        limit = max(1, min(int(limit), MAX_ROWS))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, ts, requested_model, routed_model, provider, routed_by,
                       reason, tokens_in, tokens_out, reasoning_tokens, streamed,
                       latency_ms, status, had_images, account_id, error
                FROM request_log WHERE id > ? ORDER BY id ASC LIMIT ?
                """,
                (int(after_id), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily(self, days: int = 30) -> List[Dict[str, Any]]:
        """Per-day counts for the last ``days`` days (zero-filled).

        Days with no traffic are included with zero counts so the x-axis stays
        continuous rather than compressing idle periods.
        """
        days = max(1, min(int(days), 366))
        now = time.time()
        cutoff = now - days * 86400
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT date(ts, 'unixepoch', 'localtime') AS d,
                       COUNT(*)                     AS requests,
                       COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       SUM(CASE WHEN status NOT IN {self._OK_SQL} THEN 1 ELSE 0 END) AS failed
                FROM request_log
                WHERE ts >= ?
                GROUP BY d ORDER BY d
                """,
                (cutoff,),
            ).fetchall()
        by = {
            r["d"]: (r["requests"], r["tokens_in"], r["tokens_out"], r["failed"] or 0)
            for r in rows
        }
        out: List[Dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            req, tin, tout, failed = by.get(d, (0, 0, 0, 0))
            out.append({
                "date": d, "requests": req, "tokens_in": tin,
                "tokens_out": tout, "failed": failed,
            })
        return out

    def buckets(self, minutes: int = 60, bucket_s: int = 60) -> List[Dict[str, Any]]:
        """Fixed-width time buckets over the recent past, zero-filled.

        This is the live series behind the ledger's activity chart: with
        ``minutes=60, bucket_s=60`` it yields the last hour a minute at a time,
        so a request made seconds ago is visible on the next poll. ``daily()``
        cannot do that -- a single day-wide bucket hides everything until
        tomorrow.
        """
        minutes = max(1, min(int(minutes), 1440))
        bucket_s = max(10, min(int(bucket_s), 3600))
        span = minutes * 60
        now = time.time()
        # Snap the right edge to a bucket boundary so buckets stay stable
        # between polls instead of sliding by a few hundred milliseconds.
        end = int(now // bucket_s * bucket_s) + bucket_s
        start = end - span
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT CAST(ts / ? AS INTEGER) * ? AS b,
                       COUNT(*)                     AS requests,
                       COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency_ms,
                       SUM(CASE WHEN status NOT IN {self._OK_SQL} THEN 1 ELSE 0 END) AS failed
                FROM request_log
                WHERE ts >= ?
                GROUP BY b ORDER BY b
                """,
                (bucket_s, bucket_s, start),
            ).fetchall()
        by = {int(r["b"]): r for r in rows}
        out: List[Dict[str, Any]] = []
        for t in range(start, end, bucket_s):
            r = by.get(t)
            out.append({
                "ts": t,
                "requests": r["requests"] if r else 0,
                "tokens_in": r["tokens_in"] if r else 0,
                "tokens_out": r["tokens_out"] if r else 0,
                "avg_latency_ms": round(r["avg_latency_ms"], 1) if r else 0,
                "failed": (r["failed"] or 0) if r else 0,
            })
        return out

    def prune(self, older_than_days: int) -> int:
        """Delete rows older than ``older_than_days``. Returns rows removed.

        Called on startup. Without this the request log grows without bound --
        fine at a few hundred rows, a problem at hundreds of thousands. Pass 0
        to disable.
        """
        days = int(older_than_days)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
