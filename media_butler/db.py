"""SQLite state store.

The bot must survive restarts between asking a question and receiving the
button press, and it must be able to reconcile a move that was interrupted
mid-flight.  Both need durable state; a single SQLite file (stdlib, WAL mode)
is enough.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ACTIVE_STATUSES = ("pending", "moving", "blocked")
SKIPPED_STATUSES = ("ignored", "blocked", "done")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    video_count  INTEGER NOT NULL DEFAULT 0,
    archive_path TEXT,
    status       TEXT NOT NULL,
    dest         TEXT,
    final_path   TEXT,
    message_id   INTEGER,
    error        TEXT,
    fingerprint  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS items_active_source ON items(source)
    WHERE status IN ('pending', 'moving', 'blocked');
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_archive ON items(archive_path);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

COLUMNS = {
    "source",
    "kind",
    "display_name",
    "size_bytes",
    "video_count",
    "archive_path",
    "status",
    "dest",
    "final_path",
    "message_id",
    "error",
    "fingerprint",
    "updated_at",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Item:
    id: str
    source: str
    kind: str
    display_name: str
    size_bytes: int
    video_count: int
    archive_path: str | None
    status: str
    dest: str | None
    final_path: str | None
    message_id: int | None
    error: str | None
    fingerprint: str
    created_at: str
    updated_at: str


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(**{key: row[key] for key in Item.__dataclass_fields__})


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ items
    def add_item(
        self,
        *,
        item_id: str,
        source: str,
        kind: str,
        display_name: str,
        size_bytes: int,
        video_count: int = 0,
        fingerprint: str,
        archive_path: str | None = None,
        status: str = "pending",
        error: str | None = None,
    ) -> Item | None:
        """Insert a new item, or return None if an equivalent one is active."""
        now = utcnow()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO items (id, source, kind, display_name, size_bytes,
                                       video_count, archive_path, status, fingerprint,
                                       error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        source,
                        kind,
                        display_name,
                        size_bytes,
                        video_count,
                        archive_path,
                        status,
                        fingerprint,
                        error,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(item_id)

    def get(self, item_id: str) -> Item | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def update(self, item_id: str, **fields: Any) -> None:
        unknown = set(fields) - COLUMNS
        if unknown:
            raise ValueError(f"unknown columns: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values()) + [utcnow(), item_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE items SET {assignments}, updated_at = ? WHERE id = ?",
                values,
            )
            self._conn.commit()

    def set_status(self, item_id: str, status: str, **fields: Any) -> None:
        self.update(item_id, status=status, **fields)

    def delete(self, item_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    def items_with_status(self, *statuses: str) -> list[Item]:
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM items WHERE status IN ({placeholders}) ORDER BY created_at",
                statuses,
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def should_skip(self, source: str, fingerprint: str) -> bool:
        """True when this exact source was already ignored or is blocked."""
        placeholders = ", ".join("?" for _ in SKIPPED_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1 FROM items
                WHERE source = ? AND fingerprint = ? AND status IN ({placeholders})
                LIMIT 1
                """,
                (source, fingerprint, *SKIPPED_STATUSES),
            ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS total FROM items GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total FROM items WHERE status = 'pending'"
            ).fetchone()
        return int(row["total"])

    def awaiting_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total FROM items WHERE status IN ('pending', 'blocked')"
            ).fetchone()
        return int(row["total"])

    def archive_has_open_items(self, archive_path: str) -> bool:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES + ("error",))
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1 FROM items
                WHERE archive_path = ? AND status IN ({placeholders})
                LIMIT 1
                """,
                (archive_path, *ACTIVE_STATUSES, "error"),
            ).fetchone()
        return row is not None

    def archive_has_ignored_items(self, archive_path: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM items WHERE archive_path = ? AND status = 'ignored' LIMIT 1",
                (archive_path,),
            ).fetchone()
        return row is not None

    def archive_known(self, archive_path: str, fingerprint: str) -> bool:
        """True when this exact archive already produced an item, whatever its status.

        The row is the source of truth: if it exists, the archive has already
        been extracted (or deliberately ignored/blocked), so there is no reason
        to unpack it again.  Retrying a blocked archive deletes its row first.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM items WHERE archive_path = ? AND fingerprint = ? LIMIT 1",
                (archive_path, fingerprint),
            ).fetchone()
        return row is not None

    def reconcile_moving(self, *, delete_source=None) -> dict[str, int]:
        """Repair items left in ``moving`` by a crash.

        ``delete_source`` is an optional callable used when both the final
        destination and the source survived: the destination wins and the
        leftover source is removed.
        """
        repaired = {"done": 0, "pending": 0, "error": 0}
        for item in self.items_with_status("moving"):
            source = Path(item.source)
            final = Path(item.final_path) if item.final_path else None
            final_exists = bool(final and final.exists())
            source_exists = source.exists()

            if final_exists:
                if source_exists and delete_source is not None:
                    try:
                        delete_source(source)
                    except OSError:
                        pass
                    source_exists = source.exists()
                if source_exists:
                    self.set_status(
                        item.id,
                        "error",
                        error="the destination is complete but the source could not be removed",
                    )
                    repaired["error"] += 1
                else:
                    self.set_status(item.id, "done")
                    repaired["done"] += 1
            elif source_exists:
                self.set_status(item.id, "pending", final_path=None, dest=None)
                repaired["pending"] += 1
            else:
                self.set_status(item.id, "error", error="both source and destination have disappeared")
                repaired["error"] += 1
        return repaired

    def purge_old(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM items WHERE status IN ('done', 'ignored') AND updated_at < ?",
                (cutoff,),
            )
            self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------ state
    def state_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def state_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def state_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM state WHERE key = ?", (key,))
            self._conn.commit()

    def state_clear_prefix(self, prefix: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM state WHERE key LIKE ?", (prefix + "%",))
            self._conn.commit()
