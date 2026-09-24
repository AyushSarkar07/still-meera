"""Persistence: notes, processed updates, bot-sent messages, poll offset.

Two backends share the same SQL:
- SQLite file (local long-polling mode and tests)
- Postgres via a postgres:// URL (serverless deployment, where the local disk is temporary)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# BIGINT: Telegram channel IDs (-100...) do not fit in 32 bits.
# DOUBLE PRECISION: Postgres REAL is 32-bit and would round Unix timestamps.
SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id   BIGINT PRIMARY KEY,
    seen_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    note_id     BIGINT,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per captured note. The original text/voice reference is never deleted.
-- Each stage's output is cached so a retry never re-pays for a completed stage.
CREATE TABLE IF NOT EXISTS notes (
    id              {id_column},
    chat_id         BIGINT NOT NULL,
    message_id      BIGINT NOT NULL,
    update_id       BIGINT,
    kind            TEXT NOT NULL,          -- text | voice
    original_text   TEXT,                   -- text notes, or voice caption
    voice_file_id   TEXT,
    voice_mime      TEXT,
    transcript      TEXT,
    triage_json     TEXT,
    news_json       TEXT,
    draft_json      TEXT,
    status          TEXT NOT NULL,          -- see pipeline.Status
    stage           TEXT,                   -- stage that last failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_retry_at   DOUBLE PRECISION,
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL,
    UNIQUE (chat_id, message_id)
);
"""

ID_COLUMN = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgres": "BIGSERIAL PRIMARY KEY"}


def is_postgres_url(target: Path | str) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


class Store:
    def __init__(self, target: Path | str):
        """target: a SQLite file path, ":memory:", or a postgres:// URL."""
        if is_postgres_url(target):
            import psycopg
            from psycopg.rows import dict_row

            self.dialect = "postgres"
            # prepare_threshold=None: safe behind transaction-mode poolers (Neon, Supabase).
            self.conn = psycopg.connect(str(target), autocommit=True, row_factory=dict_row,
                                        prepare_threshold=None)
        else:
            if str(target) != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self.dialect = "sqlite"
            self.conn = sqlite3.connect(str(target), isolation_level=None)  # autocommit
            self.conn.row_factory = sqlite3.Row
        schema = SCHEMA.format(id_column=ID_COLUMN[self.dialect])
        if self.dialect == "sqlite":
            self.conn.executescript(schema)
        else:
            self.conn.execute(schema)

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: tuple = ()):
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    # --- polling offset -------------------------------------------------
    def get_offset(self) -> int | None:
        row = self._exec("SELECT value FROM kv WHERE key='offset'").fetchone()
        return int(row["value"]) if row else None

    def set_offset(self, offset: int) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES('offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(offset),),
        )

    # --- update de-duplication -----------------------------------------
    def claim_update(self, update_id: int) -> bool:
        """Record an update id. Returns False if it was already seen."""
        cur = self._exec(
            "INSERT INTO processed_updates(update_id, seen_at) VALUES(?, ?) ON CONFLICT DO NOTHING",
            (update_id, time.time()),
        )
        return cur.rowcount == 1

    # --- bot output tracking (loop prevention) -------------------------
    def record_bot_message(self, chat_id: int, message_id: int, note_id: int | None) -> None:
        self._exec(
            "INSERT INTO bot_messages(chat_id, message_id, note_id) VALUES(?, ?, ?) ON CONFLICT DO NOTHING",
            (chat_id, message_id, note_id),
        )

    def is_bot_message(self, chat_id: int, message_id: int) -> bool:
        return self._exec(
            "SELECT 1 FROM bot_messages WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone() is not None

    # --- notes ----------------------------------------------------------
    def insert_note(self, **fields: Any) -> int | None:
        """Insert a note. Returns None if this chat/message was already stored."""
        now = time.time()
        fields.setdefault("status", "received")
        fields["created_at"] = fields["updated_at"] = now
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        row = self._exec(
            f"INSERT INTO notes({cols}) VALUES({marks}) ON CONFLICT DO NOTHING RETURNING id",
            tuple(fields.values()),
        ).fetchone()
        return row["id"] if row else None

    def update_note(self, note_id: int, **fields: Any) -> None:
        for k in ("triage_json", "news_json", "draft_json"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE notes SET {sets} WHERE id=?", (*fields.values(), note_id))

    def claim_note(self, note_id: int, from_status: str, to_status: str) -> bool:
        """Atomically move a note between statuses. False if another worker got there first."""
        cur = self._exec("UPDATE notes SET status=?, updated_at=? WHERE id=? AND status=?",
                         (to_status, time.time(), note_id, from_status))
        return cur.rowcount == 1

    def get_note(self, note_id: int) -> dict | None:
        row = self._exec("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return _decode(row) if row else None

    def list_notes(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self._exec(
                "SELECT * FROM notes WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            rows = self._exec("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]

    def due_retries(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        rows = self._exec(
            "SELECT * FROM notes WHERE status='retry_pending' AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY id",
            (now,),
        )
        return [_decode(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._exec("SELECT status, COUNT(*) AS n FROM notes GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def draft_posts(self) -> list[str]:
        rows = self._exec("SELECT draft_json FROM notes WHERE draft_json IS NOT NULL")
        posts = []
        for r in rows:
            try:
                posts.append(json.loads(r["draft_json"]).get("post", ""))
            except (json.JSONDecodeError, AttributeError):
                continue
        return posts

    def requeue_interrupted(self, updated_before: float | None = None) -> int:
        """Move notes stuck mid-flight back to retry_pending (no attempt is charged).

        updated_before=None requeues all of them (safe at startup of a single process).
        On serverless, pass a cutoff so notes still being processed by a live request are left alone.
        """
        sql = "UPDATE notes SET status='retry_pending', next_retry_at=NULL WHERE status IN ('processing', 'received')"
        params: tuple = ()
        if updated_before is not None:
            sql += " AND updated_at<?"
            params = (updated_before,)
        return self._exec(sql, params).rowcount


def _decode(row) -> dict:
    d = dict(row)
    for k in ("triage_json", "news_json", "draft_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
