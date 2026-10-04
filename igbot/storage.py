"""SQLite storage for uploaded media, conversations, confirmation requests and scheduled posts.

The bot is a single asyncio process and every query here is tiny, so one sqlite3 connection used
from the event-loop thread is enough.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    filename TEXT NOT NULL UNIQUE,
    preview_filename TEXT,
    width INTEGER,
    height INTEGER,
    duration REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    chat_id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    messages TEXT NOT NULL,
    context_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    spec TEXT NOT NULL,
    publish_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    ig_media_id TEXT,
    permalink TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_posts_due ON scheduled_posts (status, publish_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """UTC timestamps in one fixed format, so they also compare correctly as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaRecord:
    id: int
    chat_id: int
    kind: str  # "image" | "video"
    filename: str
    preview_filename: str | None
    width: int | None
    height: int | None
    duration: float | None
    created_at: datetime


@dataclass
class Conversation:
    chat_id: int
    fingerprint: str
    messages: list[dict[str, Any]]
    context_tokens: int
    updated_at: datetime


@dataclass
class ActionRecord:
    id: int
    chat_id: int
    kind: str
    payload: dict[str, Any]
    status: str  # pending | running | done | failed | cancelled | expired
    result: str | None
    created_at: datetime


@dataclass
class ScheduledPost:
    id: int
    chat_id: int
    spec: dict[str, Any]
    publish_at: datetime
    status: str  # scheduled | publishing | published | failed | cancelled
    ig_media_id: str | None
    permalink: str | None
    error: str | None


class Storage:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._conn:
            return self._conn.execute(sql, params)

    # --- media ---------------------------------------------------------------------------

    def add_media(
        self,
        chat_id: int,
        kind: str,
        filename: str,
        preview_filename: str | None,
        width: int | None,
        height: int | None,
        duration: float | None,
    ) -> MediaRecord:
        cursor = self._execute(
            "INSERT INTO media (chat_id, kind, filename, preview_filename, width, height, duration, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, kind, filename, preview_filename, width, height, duration, to_iso(utcnow())),
        )
        record = self.get_media(cursor.lastrowid)
        assert record is not None
        return record

    def get_media(self, media_id: int) -> MediaRecord | None:
        row = self._conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return _media(row) if row else None

    def list_media(self, chat_id: int, limit: int) -> list[MediaRecord]:
        rows = self._conn.execute(
            "SELECT * FROM media WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
        return [_media(row) for row in rows]

    def media_created_before(self, cutoff: datetime) -> list[MediaRecord]:
        rows = self._conn.execute("SELECT * FROM media WHERE created_at < ?", (to_iso(cutoff),)).fetchall()
        return [_media(row) for row in rows]

    def delete_media(self, media_id: int) -> None:
        self._execute("DELETE FROM media WHERE id = ?", (media_id,))

    def media_filenames(self) -> set[str]:
        """Every file name (media and preview) that belongs to a stored upload."""
        names: set[str] = set()
        for row in self._conn.execute("SELECT filename, preview_filename FROM media"):
            names.update(name for name in (row["filename"], row["preview_filename"]) if name)
        return names

    def media_ids_in_use(self) -> set[int]:
        """Media that a scheduled post or a pending publish request still needs."""
        specs = [
            json.loads(row["spec"])
            for row in self._conn.execute("SELECT spec FROM scheduled_posts WHERE status IN ('scheduled', 'publishing')")
        ]
        for row in self._conn.execute(
            "SELECT payload FROM actions WHERE status IN ('pending', 'running') AND kind IN ('publish', 'schedule')"
        ):
            specs.append(json.loads(row["payload"])["spec"])
        return {int(media_id) for spec in specs for media_id in spec["media_ids"]}

    # --- conversations -------------------------------------------------------------------

    def get_conversation(self, chat_id: int) -> Conversation | None:
        row = self._conn.execute("SELECT * FROM conversations WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        return Conversation(
            chat_id=row["chat_id"],
            fingerprint=row["fingerprint"],
            messages=json.loads(row["messages"]),
            context_tokens=row["context_tokens"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_conversation(self, conversation: Conversation) -> None:
        self._execute(
            "INSERT INTO conversations (chat_id, fingerprint, messages, context_tokens, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (chat_id) DO UPDATE SET fingerprint = excluded.fingerprint,"
            " messages = excluded.messages, context_tokens = excluded.context_tokens, updated_at = excluded.updated_at",
            (
                conversation.chat_id,
                conversation.fingerprint,
                json.dumps(conversation.messages, ensure_ascii=False),
                conversation.context_tokens,
                to_iso(conversation.updated_at),
            ),
        )

    def delete_conversation(self, chat_id: int) -> None:
        self._execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))

    # --- confirmation requests -----------------------------------------------------------

    def create_action(self, chat_id: int, kind: str, payload: dict[str, Any]) -> ActionRecord:
        now = to_iso(utcnow())
        cursor = self._execute(
            "INSERT INTO actions (chat_id, kind, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, kind, json.dumps(payload, ensure_ascii=False), now, now),
        )
        action = self.get_action(cursor.lastrowid)
        assert action is not None
        return action

    def get_action(self, action_id: int) -> ActionRecord | None:
        row = self._conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return _action(row) if row else None

    def set_action_status(self, action_id: int, old: str, new: str, result: str | None = None) -> bool:
        """Move a request from one status to another; False if it wasn't in `old` (already handled)."""
        cursor = self._execute(
            "UPDATE actions SET status = ?, result = ?, updated_at = ? WHERE id = ? AND status = ?",
            (new, result, to_iso(utcnow()), action_id, old),
        )
        return cursor.rowcount == 1

    def pending_actions(self, chat_id: int) -> list[ActionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE chat_id = ? AND status = 'pending' ORDER BY id", (chat_id,)
        ).fetchall()
        return [_action(row) for row in rows]

    def expire_pending_actions(self, created_before: datetime) -> int:
        cursor = self._execute(
            "UPDATE actions SET status = 'expired', updated_at = ? WHERE status = 'pending' AND created_at < ?",
            (to_iso(utcnow()), to_iso(created_before)),
        )
        return cursor.rowcount

    def fail_interrupted_actions(self) -> list[ActionRecord]:
        """Requests that were running when the process stopped; their outcome on Instagram is unknown."""
        rows = self._conn.execute("SELECT * FROM actions WHERE status = 'running'").fetchall()
        for row in rows:
            self.set_action_status(row["id"], "running", "failed", "Bot bajarish paytida to'xtab qoldi")
        return [_action(row) for row in rows]

    # --- scheduled posts -----------------------------------------------------------------

    def add_scheduled_post(self, chat_id: int, spec: dict[str, Any], publish_at: datetime) -> ScheduledPost:
        now = to_iso(utcnow())
        cursor = self._execute(
            "INSERT INTO scheduled_posts (chat_id, spec, publish_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, json.dumps(spec, ensure_ascii=False), to_iso(publish_at), now, now),
        )
        post = self.get_scheduled_post(cursor.lastrowid)
        assert post is not None
        return post

    def get_scheduled_post(self, post_id: int) -> ScheduledPost | None:
        row = self._conn.execute("SELECT * FROM scheduled_posts WHERE id = ?", (post_id,)).fetchone()
        return _scheduled(row) if row else None

    def list_scheduled_posts(self) -> list[ScheduledPost]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_posts WHERE status = 'scheduled' ORDER BY publish_at"
        ).fetchall()
        return [_scheduled(row) for row in rows]

    def due_scheduled_posts(self, now: datetime) -> list[ScheduledPost]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_posts WHERE status = 'scheduled' AND publish_at <= ? ORDER BY publish_at",
            (to_iso(now),),
        ).fetchall()
        return [_scheduled(row) for row in rows]

    def _set_post_status(self, post_id: int, old: str, new: str, **fields: Any) -> bool:
        assignments = "".join(f", {name} = ?" for name in fields)
        cursor = self._execute(
            f"UPDATE scheduled_posts SET status = ?, updated_at = ?{assignments} WHERE id = ? AND status = ?",
            (new, to_iso(utcnow()), *fields.values(), post_id, old),
        )
        return cursor.rowcount == 1

    def claim_scheduled_post(self, post_id: int) -> bool:
        return self._set_post_status(post_id, "scheduled", "publishing")

    def finish_scheduled_post(
        self, post_id: int, status: str, *, ig_media_id: str | None = None, permalink: str | None = None,
        error: str | None = None,
    ) -> None:
        self._set_post_status(
            post_id, "publishing", status, ig_media_id=ig_media_id, permalink=permalink, error=error
        )

    def cancel_scheduled_post(self, post_id: int) -> bool:
        return self._set_post_status(post_id, "scheduled", "cancelled")

    def fail_interrupted_posts(self) -> list[ScheduledPost]:
        """Posts that were being published when the process stopped; they may or may not be live."""
        rows = self._conn.execute("SELECT * FROM scheduled_posts WHERE status = 'publishing'").fetchall()
        for row in rows:
            self.finish_scheduled_post(row["id"], "failed", error="Bot joylash paytida to'xtab qoldi")
        return [_scheduled(row) for row in rows]


def _media(row: sqlite3.Row) -> MediaRecord:
    return MediaRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        kind=row["kind"],
        filename=row["filename"],
        preview_filename=row["preview_filename"],
        width=row["width"],
        height=row["height"],
        duration=row["duration"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _action(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        result=row["result"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _scheduled(row: sqlite3.Row) -> ScheduledPost:
    return ScheduledPost(
        id=row["id"],
        chat_id=row["chat_id"],
        spec=json.loads(row["spec"]),
        publish_at=datetime.fromisoformat(row["publish_at"]),
        status=row["status"],
        ig_media_id=row["ig_media_id"],
        permalink=row["permalink"],
        error=row["error"],
    )
