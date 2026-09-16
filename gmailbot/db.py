"""SQLite access layer.

Fixes over the original (see docs/IMPROVEMENTS.md):

* P0-2  One connection per thread + WAL + a write lock. The original shared a
  single connection between the IMAP poller thread and the asyncio event loop,
  which produced ``cannot commit - no transaction is active`` and silently
  dropped ~50% of inserted mail in a 2-thread reproduction.
* P0-3  Every alias has exactly one owner. ``add_message`` resolves that owner
  instead of using ``INSERT ... SELECT`` over *all* rows with that name, which
  would hand the same OTP to two users.
* P0-4  De-duplication on RFC ``Message-ID``, so a poller restart or a re-run
  can never insert the same email twice.
* P1-1  Timestamps are UTC ISO-8601 with a fixed width, so text ordering equals
  chronological ordering and the UI can render them without re-parsing.
* P1-2  Indexes on the columns every read path uses.
* P1-3  Migration via ``PRAGMA user_version`` instead of ad-hoc
  ``PRAGMA table_info`` probing on every boot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from .models import (
    Alias,
    AliasAddResult,
    LinkPattern,
    Message,
    RequiredChannel,
    StoredMessage,
    from_iso,
    to_iso,
    utcnow,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

#: Characters kept from a body when it is used purely for display.
PREVIEW_CHARS = 220


def _preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    """Collapse a body into a short single-line preview for notifications."""
    squashed = " ".join((body or "").split())
    return squashed if len(squashed) <= limit else squashed[: limit - 1] + "\u2026"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    banned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    alias_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    alias_name TEXT    NOT NULL UNIQUE,          -- one Gmail mailbox = one namespace
    format     TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_user ON aliases(user_id, active);

CREATE TABLE IF NOT EXISTS messages (
    message_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_id          INTEGER NOT NULL REFERENCES aliases(alias_id) ON DELETE CASCADE,
    rfc_message_id    TEXT    UNIQUE,            -- NULL allowed, set dedupes
    sender            TEXT,
    recipient         TEXT,
    email_subject     TEXT,
    email_body        TEXT,
    received_at       TEXT    NOT NULL,   -- from the sender's Date header (display)
    stored_at         TEXT    NOT NULL,   -- when we stored it (retention is measured here)
    seen              INTEGER NOT NULL DEFAULT 0,
    otp_code          TEXT,
    verification_links TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_alias ON messages(alias_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    feedback_text     TEXT,
    feedback_photo_id TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS required_channels (
    chat_id     TEXT PRIMARY KEY,   -- '@username' (public) or '-100...' (private)
    title       TEXT,
    invite_link TEXT,
    added_by    INTEGER,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_patterns (
    pattern  TEXT PRIMARY KEY,   -- host glob, e.g. 'url*.mail.anthropic.com'
    kind     TEXT NOT NULL,      -- 'tracker'
    source   TEXT NOT NULL,      -- 'ai' (learned) or 'admin' (added by hand)
    added_at TEXT NOT NULL,
    hits     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Thread-safe SQLite wrapper.

    ``check_same_thread`` stays at its default (True) on purpose: each thread
    gets its own connection, which is the only arrangement SQLite actually
    supports safely. Writes additionally take a process-wide lock so two
    threads cannot interleave transactions on the same file.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._bootstrap_lock = threading.Lock()
        self._bootstrap()

    # ------------------------------------------------------------- plumbing
    def _make_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,  # explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Per-thread connection (created lazily)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._make_connection()
            self._local.conn = conn
        return conn

    def _bootstrap(self) -> None:
        with self._bootstrap_lock:
            conn = self._make_connection()
            try:
                with conn:
                    existing_tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    # executescript, not split(";"): the schema comments contain
                    # semicolons and a naive split produced "incomplete input".
                    conn.executescript(SCHEMA)
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version < SCHEMA_VERSION:
                        self._migrate(conn, version, legacy=bool(existing_tables))
                        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            finally:
                conn.close()
        logger.info("database ready at %s (schema v%d)", self.path, SCHEMA_VERSION)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, wanted: dict[str, str]
    ) -> None:
        if not self._columns(conn, table):
            return  # table does not exist in this legacy database
        existing = self._columns(conn, table)
        for column, ddl_type in wanted.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    def _migrate(
        self, conn: sqlite3.Connection, from_version: int, *, legacy: bool = False
    ) -> None:
        """Additive, versioned migrations (``PRAGMA user_version`` is the cursor)."""
        if from_version < 1:
            # v0 = the original single-file bot's schema.
            self._ensure_columns(
                conn,
                "messages",
                {"rfc_message_id": "TEXT", "sender": "TEXT", "recipient": "TEXT"},
            )
            self._ensure_columns(conn, "aliases", {"format": "TEXT"})
        if from_version < 2:
            # Retention used to be measured from the Date header, so mail that had
            # been waiting in the mailbox longer than the TTL was deleted in the
            # same cycle it was stored. Track our own clock separately.
            self._ensure_columns(conn, "messages", {"stored_at": "TEXT"})
            conn.execute(
                "UPDATE messages SET stored_at = received_at WHERE stored_at IS NULL"
            )
        if legacy:
            # Fresh databases are created at the current version; only a database
            # from an earlier schema needs (and reports) a migration.
            logger.info(
                "migrated existing database from schema v%d to v%d",
                from_version,
                SCHEMA_VERSION,
            )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- users
    def ensure_user(self, user_id: int) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)",
                (user_id, to_iso(utcnow())),
            )

    def is_banned(self, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row["banned"]) if row else False

    def set_banned(self, user_id: int, banned: bool) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "INSERT INTO users (user_id, banned, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET banned = excluded.banned",
                (user_id, int(banned), to_iso(utcnow())),
            )

    def all_user_ids(self, *, include_banned: bool = False) -> list[int]:
        sql = "SELECT user_id FROM users"
        if not include_banned:
            sql += " WHERE banned = 0"
        return [row["user_id"] for row in self.conn.execute(sql)]

    # -------------------------------------------------------------- aliases
    def alias_owner(self, name: str) -> int | None:
        """User id owning ``name``, or ``None``. One owner per name, always."""
        row = self.conn.execute(
            "SELECT user_id FROM aliases WHERE alias_name = ?", (name.lower(),)
        ).fetchone()
        return row["user_id"] if row else None

    def add_alias(self, user_id: int, name: str, fmt: str | None = None) -> AliasAddResult:
        name = name.lower()
        self.ensure_user(user_id)
        with self._write_lock, self.conn as conn:
            row = conn.execute(
                "SELECT user_id FROM aliases WHERE alias_name = ?", (name,)
            ).fetchone()
            if row is not None:
                if row["user_id"] == user_id:
                    # Re-activating your own soft-deleted alias is not an error.
                    conn.execute(
                        "UPDATE aliases SET active = 1, format = COALESCE(?, format) "
                        "WHERE alias_name = ?",
                        (fmt, name),
                    )
                    return AliasAddResult(True, AliasAddResult.YOURS_ALREADY)
                return AliasAddResult(False, AliasAddResult.TAKEN)
            conn.execute(
                "INSERT INTO aliases (user_id, alias_name, format, active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (user_id, name, fmt, to_iso(utcnow())),
            )
        return AliasAddResult(True, AliasAddResult.CREATED)

    def count_aliases(self, user_id: int, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM aliases WHERE user_id = ?"
        params: list[object] = [user_id]
        if active_only:
            sql += " AND active = 1"
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def list_aliases(self, user_id: int, *, limit: int = 20) -> list[Alias]:
        rows = self.conn.execute(
            "SELECT alias_name, active, created_at, format FROM aliases "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            Alias(
                name=row["alias_name"],
                active=bool(row["active"]),
                created_at=from_iso(row["created_at"]),
                format=row["format"],
            )
            for row in rows
        ]

    def owned_alias(self, user_id: int, name: str, *, active_only: bool = True) -> Alias | None:
        sql = (
            "SELECT alias_name, active, created_at, format FROM aliases "
            "WHERE user_id = ? AND alias_name = ?"
        )
        params: list[object] = [user_id, name.lower()]
        if active_only:
            sql += " AND active = 1"
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return Alias(
            name=row["alias_name"],
            active=bool(row["active"]),
            created_at=from_iso(row["created_at"]),
            format=row["format"],
        )

    def set_alias_active(self, user_id: int, name: str, active: bool) -> bool:
        with self._write_lock, self.conn as conn:
            cursor = conn.execute(
                "UPDATE aliases SET active = ? WHERE user_id = ? AND alias_name = ?",
                (int(active), user_id, name.lower()),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------- messages
    def add_message(
        self,
        alias_name: str,
        subject: str,
        body: str,
        *,
        otp: str | None = None,
        links: list[str] | None = None,
        rfc_message_id: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        received_at: str | None = None,
    ) -> StoredMessage | None:
        """Store mail for the *single* owner of ``alias_name``.

        Returns ``None`` when the alias is unknown/inactive. Returns a
        ``StoredMessage`` with ``duplicate=True`` when the RFC Message-ID was
        already stored (no second notification is emitted for it).
        """
        alias_name = alias_name.lower()
        payload = json.dumps(links, ensure_ascii=False) if links else None
        now = to_iso(utcnow())
        received = received_at or now
        with self._write_lock, self.conn as conn:
            row = conn.execute(
                "SELECT alias_id, user_id FROM aliases "
                "WHERE alias_name = ? AND active = 1",
                (alias_name,),
            ).fetchone()
            if row is None:
                logger.warning("dropping mail for unknown/inactive alias %s", alias_name)
                return None

            if rfc_message_id:
                existing = conn.execute(
                    "SELECT message_id FROM messages WHERE rfc_message_id = ?",
                    (rfc_message_id,),
                ).fetchone()
                if existing is not None:
                    logger.info(
                        "duplicate mail ignored (Message-ID %s) for %s",
                        rfc_message_id,
                        alias_name,
                    )
                    return StoredMessage(
                        user_id=row["user_id"],
                        message_id=existing["message_id"],
                        alias=alias_name,
                        subject=subject,
                        otp=otp,
                        links=list(links or []),
                        received_at=from_iso(received),
                        preview=_preview(body),
                        duplicate=True,
                    )

            cursor = conn.execute(
                "INSERT INTO messages (alias_id, rfc_message_id, sender, recipient, "
                "email_subject, email_body, received_at, stored_at, otp_code, "
                "verification_links) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["alias_id"],
                    rfc_message_id,
                    sender,
                    recipient,
                    subject,
                    body,
                    received,
                    now,
                    otp,
                    payload,
                ),
            )
            message_id = int(cursor.lastrowid)
            user_id = int(row["user_id"])
        logger.info(
            "stored mail for %s (user %s, message %s, otp=%s)",
            alias_name,
            user_id,
            message_id,
            bool(otp),
        )
        return StoredMessage(
            user_id=user_id,
            message_id=message_id,
            alias=alias_name,
            subject=subject,
            otp=otp,
            links=list(links or []),
            received_at=from_iso(received),
            preview=_preview(body),
        )

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        try:
            links = json.loads(row["verification_links"]) if row["verification_links"] else []
        except (TypeError, ValueError):
            # Corrupt JSON must never take down a read path.
            logger.warning("unreadable verification_links on message %s", row["message_id"])
            links = []
        if not isinstance(links, list):
            links = []
        return Message(
            id=row["message_id"],
            alias=row["alias_name"],
            subject=row["email_subject"] or "",
            body=row["email_body"] or "",
            received_at=from_iso(row["received_at"]),
            seen=bool(row["seen"]),
            otp=row["otp_code"],
            links=[str(link) for link in links],
            sender=row["sender"],
        )

    _MESSAGE_SELECT = """
        SELECT m.message_id, m.email_subject, m.email_body, m.received_at, m.seen,
               m.otp_code, m.verification_links, m.sender, a.alias_name
        FROM messages m
        JOIN aliases a ON a.alias_id = m.alias_id
    """

    def recent_messages(self, user_id: int, *, limit: int = 20) -> list[Message]:
        rows = self.conn.execute(
            self._MESSAGE_SELECT
            + " WHERE a.user_id = ? AND a.active = 1"
            + " ORDER BY m.message_id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def alias_messages(
        self, user_id: int, alias_name: str, *, limit: int = 5, offset: int = 0
    ) -> list[Message]:
        """Messages for one alias, scoped to ``user_id``.

        Ownership is enforced in SQL, not in Python: a callback that carries a
        foreign alias name reads nothing.
        """
        rows = self.conn.execute(
            self._MESSAGE_SELECT
            + " WHERE a.user_id = ? AND a.alias_name = ? AND a.active = 1"
            + " ORDER BY m.message_id DESC LIMIT ? OFFSET ?",
            (user_id, alias_name.lower(), limit, offset),
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def count_alias_messages(self, user_id: int, alias_name: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages m JOIN aliases a ON a.alias_id = m.alias_id "
            "WHERE a.user_id = ? AND a.alias_name = ? AND a.active = 1",
            (user_id, alias_name.lower()),
        ).fetchone()
        return int(row["n"])

    def message_secret(
        self, user_id: int, message_id: int, field: str
    ) -> str | None:
        """Fetch an OTP / first link for a message *the user owns*.

        Replaces the original in-memory ``user_data`` cache: that dict was never
        pruned (unbounded memory growth) and lost its contents on restart, which
        silently produced no-op "Copy" buttons.
        """
        column = {"otp": "m.otp_code", "link": "m.verification_links"}.get(field)
        if column is None:
            raise ValueError(f"unsupported secret field: {field}")
        row = self.conn.execute(
            f"SELECT {column} AS value FROM messages m JOIN aliases a ON a.alias_id = m.alias_id "
            "WHERE m.message_id = ? AND a.user_id = ?",
            (message_id, user_id),
        ).fetchone()
        if row is None:
            return None
        value = row["value"]
        if field == "link":
            try:
                links = json.loads(value) if value else []
            except (TypeError, ValueError):
                return None
            return str(links[0]) if isinstance(links, list) and links else None
        return str(value) if value else None

    def mark_seen(self, user_id: int, message_id: int) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "UPDATE messages SET seen = 1 WHERE message_id = ? AND alias_id IN "
                "(SELECT alias_id FROM aliases WHERE user_id = ?)",
                (message_id, user_id),
            )

    def prune_messages(self, ttl_seconds: int) -> int:
        """Delete messages stored more than ``ttl_seconds`` ago.

        Measured from ``stored_at`` (our clock), never from the sender's ``Date``
        header: a message that waited in the mailbox longer than the TTL -- the
        normal case after a restart, given the initial lookback window -- used to
        be stored and deleted within the same poll cycle, so the user got a push
        notification for a code they could never open again.
        """
        cutoff = to_iso(utcnow())
        with self._write_lock, self.conn as conn:
            cursor = conn.execute(
                "DELETE FROM messages "
                "WHERE julianday(?) - julianday(COALESCE(stored_at, received_at)) > ?",
                (cutoff, ttl_seconds / 86400.0),
            )
            removed = cursor.rowcount or 0
        if removed:
            logger.info("pruned %d expired message(s)", removed)
        return int(removed)

    # -------------------------------------------------------------- feedback
    def save_feedback(
        self, user_id: int, text: str | None = None, photo_id: str | None = None
    ) -> int:
        with self._write_lock, self.conn as conn:
            cursor = conn.execute(
                "INSERT INTO feedback (user_id, feedback_text, feedback_photo_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, text, photo_id, to_iso(utcnow())),
            )
            return int(cursor.lastrowid)

    # --------------------------------------------------------- learned links
    def add_link_pattern(
        self, pattern: str, *, kind: str = "tracker", source: str = "ai"
    ) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "INSERT INTO link_patterns (pattern, kind, source, added_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(pattern) DO UPDATE SET kind = excluded.kind",
                (pattern.lower(), kind, source, to_iso(utcnow())),
            )

    def remove_link_pattern(self, pattern: str) -> bool:
        with self._write_lock, self.conn as conn:
            cursor = conn.execute(
                "DELETE FROM link_patterns WHERE pattern = ?", (pattern.lower(),)
            )
            return cursor.rowcount > 0

    def list_link_patterns(self, *, kind: str = "tracker") -> list[LinkPattern]:
        rows = self.conn.execute(
            "SELECT pattern, source, added_at FROM link_patterns WHERE kind = ? "
            "ORDER BY added_at DESC",
            (kind,),
        ).fetchall()
        return [
            LinkPattern(
                pattern=row["pattern"],
                source=row["source"],
                added_at=from_iso(row["added_at"]),
            )
            for row in rows
        ]

    def tracker_patterns(self) -> list[str]:
        """Host globs the extractor should treat as click wrappers."""
        return [
            row["pattern"]
            for row in self.conn.execute(
                "SELECT pattern FROM link_patterns WHERE kind = 'tracker'"
            )
        ]

    def note_pattern_hit(self, pattern: str) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "UPDATE link_patterns SET hits = hits + 1 WHERE pattern = ?",
                (pattern.lower(),),
            )

    def update_message_links(self, message_id: int, links: list[str]) -> None:
        """Persist a corrected link order (used to repair pre-fix rows on read)."""
        payload = json.dumps(links, ensure_ascii=False) if links else None
        with self._write_lock, self.conn as conn:
            conn.execute(
                "UPDATE messages SET verification_links = ? WHERE message_id = ?",
                (payload, message_id),
            )

    # -------------------------------------------------------- join-guard info
    def add_required_channel(
        self,
        chat_id: str,
        title: str = "",
        invite_link: str | None = None,
        added_by: int | None = None,
    ) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "INSERT INTO required_channels (chat_id, title, invite_link, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, "
                "invite_link = COALESCE(excluded.invite_link, required_channels.invite_link)",
                (chat_id, title, invite_link, added_by, to_iso(utcnow())),
            )

    def remove_required_channel(self, chat_id: str) -> bool:
        with self._write_lock, self.conn as conn:
            cursor = conn.execute(
                "DELETE FROM required_channels WHERE chat_id = ?", (chat_id,)
            )
            return cursor.rowcount > 0

    def list_required_channels(self) -> list[RequiredChannel]:
        rows = self.conn.execute(
            "SELECT chat_id, title, invite_link FROM required_channels ORDER BY added_at"
        ).fetchall()
        return [
            RequiredChannel(
                chat_id=row["chat_id"],
                title=row["title"] or "",
                invite_link=row["invite_link"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock, self.conn as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "users": scalar("SELECT COUNT(*) FROM users"),
            "banned": scalar("SELECT COUNT(*) FROM users WHERE banned = 1"),
            "aliases": scalar("SELECT COUNT(*) FROM aliases"),
            "active_aliases": scalar("SELECT COUNT(*) FROM aliases WHERE active = 1"),
            "messages": scalar("SELECT COUNT(*) FROM messages"),
            "feedback": scalar("SELECT COUNT(*) FROM feedback"),
        }
