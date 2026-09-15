"""Database layer: the P0 bug was ~50% of writes silently vanishing."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta

from gmailbot.db import Database
from gmailbot.models import to_iso, utcnow

ALIAS = "tiger123"


def test_alias_is_owned_by_exactly_one_user(db: Database):
    db.ensure_user(1)
    db.ensure_user(2)

    assert db.add_alias(1, ALIAS).reason == "created"
    second = db.add_alias(2, ALIAS)
    assert second.ok is False
    assert second.reason == "taken"
    assert db.alias_owner(ALIAS) == 1


def test_readding_your_own_alias_reactivates_it(db: Database):
    assert db.add_alias(1, ALIAS).ok
    assert db.set_alias_active(1, ALIAS, False)
    result = db.add_alias(1, ALIAS)
    assert result.ok and result.reason == "yours_already"
    assert db.owned_alias(1, ALIAS) is not None


def test_mail_never_leaks_between_users(db: Database):
    """The original used INSERT..SELECT across *every* row with that name."""
    db.add_alias(1, "alpha")
    db.add_alias(2, "beta")

    stored = db.add_message("alpha", "Subject", "Body", otp="1234")
    assert stored is not None and stored.user_id == 1

    assert [m.alias for m in db.recent_messages(1)] == ["alpha"]
    assert db.recent_messages(2) == []
    # A user cannot read another user's secrets even with a valid message id.
    assert db.message_secret(2, stored.message_id, "otp") is None
    assert db.message_secret(1, stored.message_id, "otp") == "1234"


def test_duplicate_message_id_is_ignored(db: Database):
    db.add_alias(1, ALIAS)
    first = db.add_message(ALIAS, "S", "B", rfc_message_id="<one@x>")
    second = db.add_message(ALIAS, "S", "B", rfc_message_id="<one@x>")

    assert first is not None and first.duplicate is False
    assert second is not None and second.duplicate is True
    assert db.stats()["messages"] == 1


def test_mail_for_unknown_or_inactive_alias_is_dropped(db: Database):
    assert db.add_message("nobody", "S", "B") is None
    db.add_alias(1, ALIAS)
    db.set_alias_active(1, ALIAS, False)
    assert db.add_message(ALIAS, "S", "B") is None


def test_corrupt_link_json_does_not_break_reads(db: Database):
    db.add_alias(1, ALIAS)
    db.add_message(ALIAS, "S", "B")
    db.conn.execute("UPDATE messages SET verification_links = 'not json'")
    db.conn.commit()
    assert db.recent_messages(1)[0].links == []


def test_prune_respects_ttl(db: Database):
    db.add_alias(1, ALIAS)
    db.add_message(ALIAS, "old", "B")
    # Age the row the way the clock would, by rewriting stored_at.
    db.conn.execute(
        "UPDATE messages SET stored_at = ? WHERE email_subject = 'old'",
        (to_iso(utcnow() - timedelta(hours=3)),),
    )
    db.conn.commit()
    db.add_message(ALIAS, "new", "B")

    assert db.prune_messages(3600) == 1
    assert [m.subject for m in db.recent_messages(1)] == ["new"]


def test_old_date_header_does_not_prune_a_freshly_stored_message(db: Database):
    """Regression: retention must use stored_at, not the sender's Date header.

    Mail that waited in the mailbox longer than the TTL (the normal case after a
    restart, because of the initial lookback window) used to be stored and
    deleted in the same poll cycle, so the push notification pointed at a code
    that no longer existed.
    """
    db.add_alias(1, ALIAS)
    stale_date = to_iso(utcnow() - timedelta(days=1))
    stored = db.add_message(ALIAS, "waited in the mailbox", "Body", received_at=stale_date)
    assert stored is not None

    assert db.prune_messages(3600) == 0
    messages = db.recent_messages(1)
    assert len(messages) == 1
    # The sender's timestamp is still what the user sees.
    assert messages[0].received_at.day == (utcnow() - timedelta(days=1)).day


def test_concurrent_writers_do_not_lose_messages(tmp_path):
    """Regression test for the 'cannot commit - no transaction is active' bug.

    The original shared one connection between the poller thread and the event
    loop: 800 concurrent inserts lost 392 rows. Any loss here fails the test.
    """
    database = Database(tmp_path / "concurrent.db")
    database.ensure_user(1)
    database.add_alias(1, ALIAS)

    errors: list[BaseException] = []
    inserts_per_writer = 400

    def writer(tag: str) -> None:
        try:
            for index in range(inserts_per_writer):
                database.add_message(ALIAS, f"{tag}-{index}", "body")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(200):
                database.recent_messages(1)
                time.sleep(0.0005)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("a",)),
        threading.Thread(target=writer, args=("b",)),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    database.close()
    assert errors == []
    assert database.stats()["messages"] == inserts_per_writer * 2


def test_wal_and_indexes_are_enabled(db: Database):
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    indexes = {
        row["name"]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_messages_alias", "idx_messages_received", "idx_aliases_user"} <= indexes


def test_legacy_database_is_migrated(tmp_path):
    """A database created by the original single-file bot must still open."""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, banned BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE aliases (alias_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                              alias_name TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                              active BOOLEAN DEFAULT TRUE);
        CREATE TABLE messages (message_id INTEGER PRIMARY KEY AUTOINCREMENT, alias_id INTEGER,
                               email_subject TEXT, email_body TEXT,
                               received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                               seen BOOLEAN DEFAULT FALSE, otp_code TEXT, verification_links TEXT);
        INSERT INTO users (user_id) VALUES (7);
        INSERT INTO aliases (user_id, alias_name) VALUES (7, 'legacyalias');
        INSERT INTO messages (alias_id, email_subject, email_body, otp_code)
            VALUES (1, 'old subject', 'old body', '9999');
        """
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    aliases = database.list_aliases(7)
    assert [a.name for a in aliases] == ["legacyalias"]
    messages = database.recent_messages(7)
    assert messages[0].subject == "old subject"
    assert messages[0].otp == "9999"
    assert database.add_message("legacyalias", "new", "b", rfc_message_id="<x@y>") is not None
    database.close()


def test_timestamps_sort_chronologically(db: Database):
    assert to_iso(utcnow()) < to_iso(utcnow() + timedelta(seconds=1))


def test_set_banned_creates_missing_user(db: Database):
    db.set_banned(99, True)
    assert db.is_banned(99)
    db.set_banned(99, False)
    assert not db.is_banned(99)
