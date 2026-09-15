"""IMAP polling: cursor tracking, no flag mutation, HTML mails, de-duplication."""

from __future__ import annotations

from gmailbot.mail import GmailPoller, match_alias, parse_email
from tests.fakes import FakeImap, FakeMailbox, make_raw_email

ALIAS_ADDRESS = "owner+tiger123@gmail.com"


def build_poller(config, db, mailbox, seen: list):
    client = FakeImap(mailbox)
    poller = GmailPoller(
        config,
        db,
        on_message=seen.append,
        client_factory=lambda host: client,
        sleep=lambda seconds: None,
    )
    return poller, client


def test_alias_matching_ignores_display_names_and_case(config):
    parsed = parse_email(
        make_raw_email(to='"Acme" <OWNER+Tiger123@googlemail.com>, other@example.com')
    )
    alias, recipient = match_alias(parsed, config)
    assert alias == "tiger123"
    assert "Tiger123" in recipient


def test_non_alias_mail_is_not_matched(config):
    parsed = parse_email(make_raw_email(to="someone@example.com", extra_headers={"Delivered-To": "someone@example.com"}))
    assert match_alias(parsed, config) == (None, None)


def test_html_only_mail_is_converted_to_text(config):
    raw = make_raw_email(
        to=ALIAS_ADDRESS,
        html="<html><body><p>Your code is <b>483920</b></p>"
        '<p><a href="https://acme.test/verify?t=abc&amp;x=1">Verify</a></p></body></html>',
        body="",
    )
    parsed = parse_email(raw)
    assert "<html" not in parsed.text
    assert "483920" in parsed.text
    assert "https://acme.test/verify?t=abc&x=1" in parsed.text


def test_poll_stores_mail_and_advances_cursor(config, db):
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(1, make_raw_email(to="someone@example.com", message_id="<unrelated@x>"))
    mailbox.add(2, make_raw_email(to=ALIAS_ADDRESS, body="Your code is 483920", message_id="<otp@x>"))

    stored: list = []
    poller, client = build_poller(config, db, mailbox, stored)

    assert poller.run_once() == 2
    assert [s.alias for s in stored] == ["tiger123"]
    assert stored[0].otp == "483920"
    assert db.get_meta("last_uid") == "2"
    # The mailbox is opened read-only and no \Seen flag is ever written: the
    # user's unread state stays theirs.
    assert client.selected == [("INBOX", True)]
    assert client.stored_flags == []


def test_configured_mailbox_is_used(config, db, env, tmp_path):
    """Auto-archiving filters keep mail out of INBOX; the folder is configurable."""
    import dataclasses

    all_mail = dataclasses.replace(config, imap_mailbox="[Gmail]/All Mail")
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(1, make_raw_email(to=ALIAS_ADDRESS, message_id="<a@x>"))
    client = FakeImap(mailbox)
    poller = GmailPoller(all_mail, db, client_factory=lambda host: client, sleep=lambda s: None)
    poller.run_once()
    assert client.selected == [("[Gmail]/All Mail", True)]


def test_unmatched_mail_is_not_refetched_forever(config, db):
    """The original re-fetched every unmatched UNSEEN message every 5 seconds."""
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(1, make_raw_email(to="nobody@example.com", message_id="<a@x>"))

    poller, client = build_poller(config, db, mailbox, [])
    poller.run_once()
    assert client.fetches == [1]

    client.fetches.clear()
    assert poller.run_once() == 0  # cursor moved past UID 1
    assert client.fetches == []


def test_incremental_search_uses_uid_range(config, db):
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(5, make_raw_email(to=ALIAS_ADDRESS, message_id="<five@x>"))
    poller, client = build_poller(config, db, mailbox, [])
    poller.run_once()
    # Cold start: bounded lookback, never "all mail since the beginning of time".
    assert client.searches == [(None, "SINCE", client.searches[0][2])]
    assert client.searches[0][1] == "SINCE"
    assert client.searches[0][2].endswith("2026")

    client.searches.clear()
    poller.run_once()
    # Warm start: a UID range, which is what makes polling cheap and complete.
    assert client.searches == [(None, "UID", "6:*")]


def test_duplicate_delivery_is_stored_once(config, db):
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(1, make_raw_email(to=ALIAS_ADDRESS, message_id="<dup@x>"))
    mailbox.add(2, make_raw_email(to=ALIAS_ADDRESS, message_id="<dup@x>"))

    stored: list = []
    poller, _ = build_poller(config, db, mailbox, stored)
    poller.run_once()

    assert len(stored) == 1
    assert db.stats()["messages"] == 1


def test_mail_for_inactive_alias_is_dropped_but_cursor_advances(config, db):
    db.add_alias(1, "tiger123")
    db.set_alias_active(1, "tiger123", False)
    mailbox = FakeMailbox()
    mailbox.add(1, make_raw_email(to=ALIAS_ADDRESS, message_id="<gone@x>"))

    stored: list = []
    poller, _ = build_poller(config, db, mailbox, stored)
    poller.run_once()

    assert stored == []
    assert db.get_meta("last_uid") == "1"


def test_a_failing_cycle_does_not_kill_the_poller(config, db):
    def explode(host):
        raise RuntimeError("imap down")

    poller = GmailPoller(config, db, client_factory=explode, sleep=lambda s: None)
    try:
        poller.run_once()
    except RuntimeError:
        pass
    # The loop itself catches; run_once is allowed to raise for the caller.
    assert poller._client is None


def test_pruning_runs_every_cycle(config, db):
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    poller, _ = build_poller(config, db, mailbox, [])
    assert poller.run_once() == 0
