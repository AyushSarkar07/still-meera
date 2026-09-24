"""SQLite persistence: notes, processed updates, bot-sent messages, poll offset."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id   INTEGER PRIMARY KEY,
    seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    note_id     INTEGER,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per captured note. The original text/voice reference is never deleted.
-- Each stage's output is cached so a retry never re-pays for a completed stage.
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    update_id       INTEGER,
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
    next_retry_at   REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (chat_id, message_id)
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- polling offset -------------------------------------------------
    def get_offset(self) -> int | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key='offset'").fetchone()
        return int(row["value"]) if row else None

    def set_offset(self, offset: int) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES('offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(offset),),
        )
        self.conn.commit()

    # --- update de-duplication -----------------------------------------
    def claim_update(self, update_id: int) -> bool:
        """Record an update id. Returns False if it was already seen."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processed_updates(update_id, seen_at) VALUES(?, ?)",
            (update_id, time.time()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # --- bot output tracking (loop prevention) -------------------------
    def record_bot_message(self, chat_id: int, message_id: int, note_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO bot_messages(chat_id, message_id, note_id) VALUES(?, ?, ?)",
            (chat_id, message_id, note_id),
        )
        self.conn.commit()

    def is_bot_message(self, chat_id: int, message_id: int) -> bool:
        return self.conn.execute(
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
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO notes({cols}) VALUES({marks})", tuple(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None

    def update_note(self, note_id: int, **fields: Any) -> None:
        for k in ("triage_json", "news_json", "draft_json"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE notes SET {sets} WHERE id=?", (*fields.values(), note_id))
        self.conn.commit()

    def get_note(self, note_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return _decode(row) if row else None

    def list_notes(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM notes WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            rows = self.conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]

    def due_retries(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE status='retry_pending' AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY id",
            (now,),
        )
        return [_decode(r) for r in rows]


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("triage_json", "news_json", "draft_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
