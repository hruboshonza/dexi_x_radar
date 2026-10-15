"""The radar's own SQLite store. It is the only database the radar ever opens.

What it remembers:
1. cursors     where each X source stopped (since_id), so nothing is read or paid twice
2. posts       the queue; a post id is the primary key, so a post reaches the phone once
3. drafts      every draft round per post (a Telegram button carries only the post id)
4. replies     what the owner actually posted, matched to the drafts
5. feedback    skip reasons, edit instructions, regenerate requests
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    handle TEXT PRIMARY KEY,          -- lowercase
    id TEXT NOT NULL,
    username TEXT NOT NULL,
    followers INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,             -- watch | mention
    author TEXT NOT NULL,
    author_id TEXT,
    author_followers INTEGER,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT NOT NULL,
    lang TEXT,
    media TEXT NOT NULL DEFAULT '[]',
    urls TEXT NOT NULL DEFAULT '[]',
    addresses TEXT NOT NULL DEFAULT '[]',
    metrics TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | drafted | sent | skipped | ignored
    note TEXT,                                -- Claude: reply type and the data used
    skip_by TEXT,                             -- owner | claude
    skip_reason TEXT,
    tg_message_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, created_at);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(id),
    round INTEGER NOT NULL,
    label TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_post ON drafts(post_id, round);
CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY,              -- the owner's reply on X
    post_id TEXT NOT NULL,            -- what it replied to (may be outside the queue)
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    matched TEXT NOT NULL,            -- A | B~ | merged:AB | own | unqueued
    similarity REAL,
    diff TEXT,                        -- JSON {"added": [...], "removed": [...]}
    metrics TEXT,                     -- JSON, 24 h after posting
    author_replied INTEGER,
    checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_post ON replies(post_id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    kind TEXT NOT NULL,               -- skip | edit | regen
    value TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_post ON feedback(post_id);
"""

JSON_COLUMNS = ("media", "urls", "addresses", "metrics")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)

    def _q(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _x(self, sql: str, params=()) -> int:
        with self._lock:
            return self._db.execute(sql, params).rowcount

    # ── cursors ────────────────────────────────────────────────────────────────
    def cursor(self, name: str) -> str | None:
        rows = self._q("SELECT value FROM cursors WHERE name = ?", (name,))
        return rows[0]["value"] if rows else None

    def set_cursor(self, name: str, value: str) -> None:
        self._x(
            "INSERT INTO cursors (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, value, now_iso()),
        )

    # ── users ──────────────────────────────────────────────────────────────────
    def user(self, handle: str) -> dict | None:
        rows = self._q("SELECT * FROM users WHERE handle = ?", (handle.lower(),))
        return dict(rows[0]) if rows else None

    def put_user(self, user_id: str, username: str, followers: int | None) -> None:
        self._x(
            "INSERT INTO users (handle, id, username, followers, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(handle) DO UPDATE SET id = excluded.id, username = excluded.username, "
            "followers = excluded.followers, updated_at = excluded.updated_at",
            (username.lower(), user_id, username, followers, now_iso()),
        )

    # ── posts ──────────────────────────────────────────────────────────────────
    def insert_post(self, post: dict) -> bool:
        """True when the post is new. An id seen before is never queued again."""
        row = {**post}
        for col in JSON_COLUMNS:
            row[col] = json.dumps(row.get(col) or ([] if col != "metrics" else {}), ensure_ascii=False)
        stamp = now_iso()
        return self._x(
            "INSERT OR IGNORE INTO posts (id, source, author, author_id, author_followers, created_at, text, "
            "url, lang, media, urls, addresses, metrics, fetched_at, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (row["id"], row["source"], row["author"], row.get("author_id"), row.get("author_followers"),
             row["created_at"], row["text"], row["url"], row.get("lang"), row["media"], row["urls"],
             row["addresses"], row["metrics"], stamp, stamp),
        ) == 1

    def post(self, post_id: str) -> dict | None:
        rows = self._q("SELECT * FROM posts WHERE id = ?", (post_id,))
        return _post_dict(rows[0]) if rows else None

    def posts(self, status: str | None = None, limit: int = 20, newest_first: bool = True) -> list[dict]:
        order = "DESC" if newest_first else "ASC"
        if status:
            rows = self._q(f"SELECT * FROM posts WHERE status = ? ORDER BY created_at {order} LIMIT ?", (status, limit))
        else:
            rows = self._q(f"SELECT * FROM posts ORDER BY created_at {order} LIMIT ?", (limit,))
        return [_post_dict(r) for r in rows]

    def set_status(self, post_id: str, status: str, **fields) -> None:
        allowed = {"note", "skip_by", "skip_reason", "tg_message_id"}
        sets = ["status = ?", "updated_at = ?"]
        params = [status, now_iso()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            sets.append(f"{key} = ?")
            params.append(value)
        self._x(f"UPDATE posts SET {', '.join(sets)} WHERE id = ?", (*params, post_id))

    def set_message_id(self, post_id: str, message_id: int) -> None:
        self._x("UPDATE posts SET tg_message_id = ? WHERE id = ?", (message_id, post_id))

    def stale_open_posts(self, older_than_hours: float) -> list[dict]:
        """Queued posts the owner never answered within the window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._q(
            "SELECT * FROM posts WHERE status IN ('pending', 'drafted') AND created_at < ?", (cutoff,)
        )
        return [_post_dict(r) for r in rows]

    # ── drafts ─────────────────────────────────────────────────────────────────
    def save_drafts(self, post_id: str, texts: list[str]) -> int:
        rows = self._q("SELECT COALESCE(MAX(round), 0) AS r FROM drafts WHERE post_id = ?", (post_id,))
        rnd = rows[0]["r"] + 1
        stamp = now_iso()
        with self._lock:
            for i, text in enumerate(texts):
                self._db.execute(
                    "INSERT INTO drafts (post_id, round, label, text, created_at) VALUES (?, ?, ?, ?, ?)",
                    (post_id, rnd, "ABC"[i], text, stamp),
                )
        return rnd

    def latest_drafts(self, post_id: str) -> list[dict]:
        rows = self._q(
            "SELECT label, text, round FROM drafts WHERE post_id = ? "
            "AND round = (SELECT MAX(round) FROM drafts WHERE post_id = ?) ORDER BY label",
            (post_id, post_id),
        )
        return [dict(r) for r in rows]

    def all_drafts(self, post_id: str) -> list[dict]:
        rows = self._q("SELECT label, text, round FROM drafts WHERE post_id = ? ORDER BY round, label", (post_id,))
        return [dict(r) for r in rows]

    # ── replies ────────────────────────────────────────────────────────────────
    def insert_reply(self, reply: dict) -> bool:
        return self._x(
            "INSERT OR IGNORE INTO replies (id, post_id, text, created_at, matched, similarity, diff) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (reply["id"], reply["post_id"], reply["text"], reply["created_at"], reply["matched"],
             reply.get("similarity"), json.dumps(reply.get("diff"), ensure_ascii=False)),
        ) == 1

    def replies(self, post_id: str | None = None, since: str | None = None) -> list[dict]:
        if post_id:
            rows = self._q("SELECT * FROM replies WHERE post_id = ? ORDER BY created_at", (post_id,))
        elif since:
            rows = self._q("SELECT * FROM replies WHERE created_at >= ? ORDER BY created_at", (since,))
        else:
            rows = self._q("SELECT * FROM replies ORDER BY created_at DESC LIMIT 100")
        return [_reply_dict(r) for r in rows]

    def replies_due_for_outcome(self, older_than_hours: float) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._q(
            "SELECT * FROM replies WHERE checked_at IS NULL AND matched != 'unqueued' AND created_at < ?",
            (cutoff,),
        )
        return [_reply_dict(r) for r in rows]

    def set_outcome(self, reply_id: str, metrics: dict, author_replied: bool | None) -> None:
        self._x(
            "UPDATE replies SET metrics = ?, author_replied = ?, checked_at = ? WHERE id = ?",
            (json.dumps(metrics), None if author_replied is None else int(author_replied), now_iso(), reply_id),
        )

    # ── feedback ───────────────────────────────────────────────────────────────
    def add_feedback(self, post_id: str, kind: str, value: str | None = None) -> None:
        self._x(
            "INSERT INTO feedback (post_id, kind, value, created_at) VALUES (?, ?, ?, ?)",
            (post_id, kind, value, now_iso()),
        )

    def feedback(self, post_id: str | None = None, since: str | None = None) -> list[dict]:
        if post_id:
            rows = self._q("SELECT * FROM feedback WHERE post_id = ? ORDER BY created_at", (post_id,))
        else:
            rows = self._q("SELECT * FROM feedback WHERE created_at >= ? ORDER BY created_at", (since or "",))
        return [dict(r) for r in rows]

    def status_counts(self, since: str | None = None) -> dict:
        rows = self._q(
            "SELECT status, COUNT(*) AS n FROM posts WHERE fetched_at >= ? GROUP BY status", (since or "",)
        )
        return {r["status"]: r["n"] for r in rows}


def _post_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in JSON_COLUMNS:
        d[col] = json.loads(d[col]) if d.get(col) else ([] if col != "metrics" else {})
    return d


def _reply_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in ("diff", "metrics"):
        d[col] = json.loads(d[col]) if d.get(col) else None
    return d
