"""Matins storage: feeds, the built-in read-later queue, issues, delivery.

Deliberately a separate class from `later_ink.store` even though both open the
same SQLite file: upstream owns the users/rate-limit schema and changes it
often, and a fork that edits those tables turns every upstream commit into a
merge conflict. Same file, same WAL, disjoint tables.

Nothing here is account content by default — feeds list URLs the user chose,
and the read-later queue stores links, not article bodies. The one secret kept
is the webhook signing key, Fernet-encrypted with the app key so a stolen copy
of app.db cannot be used to forge deliveries to a user's endpoint.
"""

import hashlib
import os
import secrets as pysecrets
import sqlite3
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class Feed:
    id: str
    url: str
    title: str


@dataclass(frozen=True)
class SavedItem:
    id: int
    url: str
    title: str | None
    added_at: float


@dataclass(frozen=True)
class Issue:
    id: str
    secret: str
    title: str
    created_at: float
    path: str
    article_count: int
    word_count: int


@dataclass(frozen=True)
class Delivery:
    secret: str
    webhook_url: str | None
    webhook_secret: str | None
    email: str | None
    schedule: str
    timezone: str
    enabled: bool


def feed_id(secret: str, url: str) -> str:
    """Stable short id for one subscription. Short because it travels in OPDS
    folder URLs that get typed on an e-ink keyboard."""
    return hashlib.sha1(f"{secret}\x00{url}".encode()).hexdigest()[:12]


class MatinsStore:
    def __init__(self, path: str, fernet: Fernet):
        self.path = path
        self._fernet = fernet
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    id TEXT PRIMARY KEY,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL,
                    UNIQUE (secret, url)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS feeds_by_user ON feeds (secret)")
            # The queue a user fills with the share sheet or POST /{secret}/save.
            # consumed_by records which issue took the item, so a link is never
            # printed twice even across restarts.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saved (
                    id INTEGER PRIMARY KEY,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    added_at REAL NOT NULL,
                    consumed_by TEXT,
                    UNIQUE (secret, url)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS saved_pending ON saved (secret, consumed_by)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    secret TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    local_day TEXT NOT NULL,
                    path TEXT NOT NULL,
                    article_count INTEGER NOT NULL,
                    word_count INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS issues_by_user ON issues (secret, created_at)"
            )
            # What went into each issue. Doubles as the dedupe set for feed
            # articles, whose upstream ids are derived from the link and so stay
            # stable between fetches.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_items (
                    issue_id TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    section TEXT NOT NULL,
                    PRIMARY KEY (issue_id, article_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS issue_items_seen"
                " ON issue_items (secret, article_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery (
                    secret TEXT PRIMARY KEY,
                    webhook_url TEXT,
                    webhook_secret TEXT,
                    email TEXT,
                    schedule TEXT NOT NULL DEFAULT '07:00',
                    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ---------------------------------------------------------------- feeds

    def add_feed(self, secret: str, url: str, title: str = "") -> Feed:
        fid = feed_id(secret, url)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO feeds (id, secret, url, title, added_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (secret, url) DO UPDATE SET title = excluded.title",
                (fid, secret, url, title, time.time()),
            )
        return Feed(id=fid, url=url, title=title)

    def list_feeds(self, secret: str) -> list[Feed]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, url, title FROM feeds WHERE secret = ? ORDER BY added_at",
                (secret,),
            ).fetchall()
        return [Feed(id=r["id"], url=r["url"], title=r["title"]) for r in rows]

    def get_feed(self, secret: str, fid: str) -> Feed | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, url, title FROM feeds WHERE secret = ? AND id = ?",
                (secret, fid),
            ).fetchone()
        return Feed(id=row["id"], url=row["url"], title=row["title"]) if row else None

    def rename_feed(self, secret: str, fid: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE feeds SET title = ? WHERE secret = ? AND id = ?",
                (title, secret, fid),
            )

    def delete_feed(self, secret: str, fid: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM feeds WHERE secret = ? AND id = ?", (secret, fid)
            )
        return cur.rowcount > 0

    # -------------------------------------------------------- saved queue

    def save_url(self, secret: str, url: str, title: str | None = None) -> bool:
        """Queue a link. True if it was new, False if already queued.

        Re-saving a consumed link is allowed and re-queues it: the user's intent
        is "put this back in", and silently ignoring that looks broken."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, consumed_by FROM saved WHERE secret = ? AND url = ?",
                (secret, url),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO saved (secret, url, title, added_at) VALUES (?, ?, ?, ?)",
                    (secret, url, title, time.time()),
                )
                return True
            if row["consumed_by"] is not None:
                conn.execute(
                    "UPDATE saved SET consumed_by = NULL, added_at = ? WHERE id = ?",
                    (time.time(), row["id"]),
                )
                return True
            return False

    def list_saved(self, secret: str, include_consumed: bool = False) -> list[SavedItem]:
        sql = "SELECT id, url, title, added_at FROM saved WHERE secret = ?"
        if not include_consumed:
            sql += " AND consumed_by IS NULL"
        sql += " ORDER BY added_at"
        with self._conn() as conn:
            rows = conn.execute(sql, (secret,)).fetchall()
        return [
            SavedItem(id=r["id"], url=r["url"], title=r["title"], added_at=r["added_at"])
            for r in rows
        ]

    def get_saved(self, secret: str, saved_id: int) -> SavedItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, url, title, added_at FROM saved WHERE secret = ? AND id = ?",
                (secret, saved_id),
            ).fetchone()
        if row is None:
            return None
        return SavedItem(
            id=row["id"], url=row["url"], title=row["title"], added_at=row["added_at"]
        )

    def consume_saved(self, secret: str, ids: list[int], issue_id: str) -> None:
        if not ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE saved SET consumed_by = ? WHERE secret = ? AND id = ?",
                [(issue_id, secret, i) for i in ids],
            )

    # -------------------------------------------------------------- issues

    def new_issue_id(self) -> str:
        return f"{time.strftime('%Y%m%d')}-{pysecrets.token_hex(3)}"

    def record_issue(
        self,
        *,
        issue_id: str,
        secret: str,
        title: str,
        local_day: str,
        path: str,
        article_count: int,
        word_count: int,
        items: list[tuple[str, str, str | None, str]],
    ) -> Issue:
        created = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO issues (id, secret, title, created_at, local_day, path,"
                " article_count, word_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    secret,
                    title,
                    created,
                    local_day,
                    path,
                    article_count,
                    word_count,
                ),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO issue_items"
                " (issue_id, secret, article_id, title, url, section)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(issue_id, secret, aid, at, au, sec) for aid, at, au, sec in items],
            )
        return Issue(
            id=issue_id,
            secret=secret,
            title=title,
            created_at=created,
            path=path,
            article_count=article_count,
            word_count=word_count,
        )

    def list_issues(self, secret: str, limit: int = 50) -> list[Issue]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM issues WHERE secret = ? ORDER BY created_at DESC LIMIT ?",
                (secret, limit),
            ).fetchall()
        return [_issue(r) for r in rows]

    def get_issue(self, secret: str, issue_id: str) -> Issue | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM issues WHERE secret = ? AND id = ?", (secret, issue_id)
            ).fetchone()
        return _issue(row) if row else None

    def delete_issue(self, secret: str, issue_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM issue_items WHERE issue_id = ? AND secret = ?",
                (issue_id, secret),
            )
            conn.execute(
                "DELETE FROM issues WHERE id = ? AND secret = ?", (issue_id, secret)
            )

    def has_issue_for_day(self, secret: str, local_day: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM issues WHERE secret = ? AND local_day = ?",
                (secret, local_day),
            ).fetchone()
        return row is not None

    def issued_articles(self, secret: str, article_ids: list[str]) -> set[str]:
        """Which of `article_ids` already appeared in an issue.

        Chunked because SQLite's parameter limit is 999 by default and a large
        feed set can exceed that in one call."""
        seen: set[str] = set()
        for i in range(0, len(article_ids), 500):
            chunk = article_ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT article_id FROM issue_items WHERE secret = ?"
                    f" AND article_id IN ({placeholders})",
                    (secret, *chunk),
                ).fetchall()
            seen.update(r["article_id"] for r in rows)
        return seen

    # ------------------------------------------------------------ delivery

    def set_delivery(
        self,
        secret: str,
        *,
        webhook_url: str | None,
        webhook_secret: str | None,
        email: str | None,
        schedule: str,
        timezone: str,
        enabled: bool,
    ) -> None:
        secret_blob = (
            self._fernet.encrypt(webhook_secret.encode()).decode() if webhook_secret else None
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO delivery (secret, webhook_url, webhook_secret, email,"
                " schedule, timezone, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (secret) DO UPDATE SET"
                " webhook_url = excluded.webhook_url,"
                " webhook_secret = excluded.webhook_secret,"
                " email = excluded.email,"
                " schedule = excluded.schedule,"
                " timezone = excluded.timezone,"
                " enabled = excluded.enabled",
                (
                    secret,
                    _blank_to_none(webhook_url),
                    secret_blob,
                    _blank_to_none(email),
                    schedule,
                    timezone,
                    int(enabled),
                ),
            )

    def get_delivery(self, secret: str) -> Delivery | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM delivery WHERE secret = ?", (secret,)
            ).fetchone()
        return _delivery(row, self._fernet) if row else None

    def list_deliveries(self) -> list[Delivery]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM delivery WHERE enabled = 1").fetchall()
        return [_delivery(r, self._fernet) for r in rows]


def _issue(row: sqlite3.Row) -> Issue:
    return Issue(
        id=row["id"],
        secret=row["secret"],
        title=row["title"],
        created_at=row["created_at"],
        path=row["path"],
        article_count=row["article_count"],
        word_count=row["word_count"],
    )


def _delivery(row: sqlite3.Row, fernet: Fernet) -> Delivery:
    webhook_secret: str | None = None
    if row["webhook_secret"]:
        try:
            webhook_secret = fernet.decrypt(row["webhook_secret"].encode()).decode()
        except InvalidToken:
            # Same failure mode as a changed app key on the token store: the
            # row survives, the secret is gone. Deliveries fail cleanly rather
            # than signing with an empty key, which the receiver would reject
            # anyway.
            webhook_secret = None
    return Delivery(
        secret=row["secret"],
        webhook_url=row["webhook_url"],
        webhook_secret=webhook_secret,
        email=row["email"],
        schedule=row["schedule"],
        timezone=row["timezone"],
        enabled=bool(row["enabled"]),
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
