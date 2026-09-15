"""SQLite cache of summaries, so reruns are incremental.

Keyed on (thread_id, last_message_id): a thread that gets a new reply is stale
and gets re-summarized, an untouched thread is skipped.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import IssueSummary

_SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    thread_id        TEXT PRIMARY KEY,
    last_message_id  TEXT NOT NULL,
    subject          TEXT NOT NULL,
    category         TEXT NOT NULL,
    severity         TEXT NOT NULL,
    summarized_at    TEXT NOT NULL,
    model            TEXT NOT NULL,
    payload          TEXT NOT NULL,
    -- The thread's span. Distinct from summarized_at, which is when this tool
    -- ran. Date filters test whether this span overlaps the window, matching
    -- Gmail's own after:/before: semantics of "any message in range".
    first_message_at TEXT NOT NULL DEFAULT '',
    last_message_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_summaries_severity ON summaries(severity);
CREATE INDEX IF NOT EXISTS idx_summaries_at ON summaries(summarized_at);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add last_message_at to stores written before it existed, and backfill
        it from the payload rather than forcing a re-summarize."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(summaries)")}
        for col in ("last_message_at", "first_message_at"):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE summaries ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_msg_at "
            "ON summaries(last_message_at, first_message_at)"
        )
        for row in self._conn.execute(
            "SELECT thread_id, payload FROM summaries "
            "WHERE last_message_at = '' OR first_message_at = ''"
        ).fetchall():
            payload = json.loads(row["payload"]) or {}
            last = payload.get("last_message_at") or ""
            # Rows written before first_message_at existed have only the last
            # date; using it for both makes the span a point, which is correct
            # for single-message threads and conservative for the rest.
            first = payload.get("first_message_at") or last
            if last or first:
                self._conn.execute(
                    "UPDATE summaries SET last_message_at = ?, first_message_at = ? "
                    "WHERE thread_id = ?",
                    (last, first, row["thread_id"]),
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def is_current(self, thread_id: str, last_message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT last_message_id FROM summaries WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return bool(row) and row["last_message_id"] == last_message_id

    def upsert(self, summary: IssueSummary, last_message_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO summaries (thread_id, last_message_id, subject, category,
                                   severity, summarized_at, model, payload,
                                   first_message_at, last_message_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                subject         = excluded.subject,
                category        = excluded.category,
                severity        = excluded.severity,
                summarized_at   = excluded.summarized_at,
                model           = excluded.model,
                payload          = excluded.payload,
                first_message_at = excluded.first_message_at,
                last_message_at  = excluded.last_message_at
            """,
            (
                summary.thread_id,
                last_message_id,
                summary.subject,
                summary.category,
                summary.severity,
                datetime.now(timezone.utc).isoformat(),
                summary.model,
                json.dumps(summary.to_dict(), ensure_ascii=False),
                summary.first_message_at or summary.last_message_at or "",
                summary.last_message_at or "",
            ),
        )
        self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM summaries ORDER BY summarized_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def query(
        self,
        *,
        severities: list[str] | None = None,
        categories: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Filter cached summaries.

        `after`/`before` are YYYY-MM-DD and select threads with any message in
        the window -- not threads summarized then. `before` is exclusive.
        """
        sql = ["SELECT payload FROM summaries WHERE 1=1"]
        args: list[Any] = []

        if severities:
            sql.append(f"AND severity IN ({','.join('?' * len(severities))})")
            args.extend(s.lower() for s in severities)
        if categories:
            sql.append(f"AND category IN ({','.join('?' * len(categories))})")
            args.extend(categories)
        # Overlap, not containment: a thread that opened on the 15th and was
        # answered on the 18th belongs to both days, which is what Gmail's own
        # after:/before: search returns.
        if after:
            sql.append("AND last_message_at >= ?")
            args.append(after)
        if before:
            # Exclusive. A datetime on that day sorts after the bare date, so
            # comparing against the plain date excludes the whole day.
            sql.append("AND first_message_at < ?")
            args.append(before)
        if search:
            # payload holds the whole summary, so this covers subject, issue,
            # summary text and identifiers in one pass.
            sql.append("AND payload LIKE ?")
            args.append(f"%{search}%")

        sql.append("ORDER BY severity ASC, summarized_at DESC LIMIT ?")
        args.append(limit)

        rows = self._conn.execute(" ".join(sql), args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def counts_by(self, field: str) -> dict[str, int]:
        if field not in ("severity", "category"):
            raise ValueError("field must be 'severity' or 'category'")
        rows = self._conn.execute(
            f"SELECT {field} AS k, COUNT(*) AS n FROM summaries GROUP BY {field} ORDER BY n DESC"
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def total(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0])

    def get(self, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM summaries WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None
