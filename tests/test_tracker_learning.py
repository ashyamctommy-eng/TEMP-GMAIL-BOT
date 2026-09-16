"""AI-assisted tracker learning.

The design point being tested: the model is a *teacher*. It classifies one
ambiguous mail, the wrapper's host pattern is stored, and from then on the
deterministic rules handle it — offline, instantly, and without another call.
"""

from __future__ import annotations

import asyncio

from gmailbot.ai import redact_url
from gmailbot.links import extract_link_urls, extract_links, learn_host_pattern
from gmailbot.mail import GmailPoller
from tests.fakes import FakeBot, FakeMailbox, FakeImap, make_raw_email

ALIAS = "owner+claude@gmail.com"
MAGIC = (
    "https://app.acme.test/magic-link#34a2fff8c71e4bfd2cbaf46f4271d5af:"
    "d2lsZHBoYXJtdGVjaDkrY2xhdWRlQGdtYWlsLmNvbQ=="
)
#: A wrapper family the built-in rules do NOT know, so only learning can catch it.
#: It carries "confirm/click" in the path, like real trackers do -- which is also
#: why it survives the minimum-score floor and reaches the judge at all.
NOVEL_TRACKER = "https://links.acme-campaign.net/confirm/click?e=email&u=42"


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- redaction
def test_redaction_removes_everything_secret():
    redacted = redact_url(MAGIC)
    assert "34a2fff8" not in redacted, "the magic-link token must never be sent"
    assert redacted == "https://app.acme.test/magic-link#<redacted>"


def test_redaction_keeps_the_classification_signal():
    redacted = redact_url(NOVEL_TRACKER)
    # host, path and parameter names survive -- that is what the judge needs
    assert "links.acme-campaign.net" in redacted
    assert "/confirm/click" in redacted
    assert "e=<redacted>" in redacted and "u=<redacted>" in redacted
    assert "email" not in redacted and "42" not in redacted


def test_redaction_scrubs_long_path_segments():
    url = "https://x.test/" + "a" * 80 + "/verify?token=secret"
    redacted = redact_url(url)
    assert "a" * 80 not in redacted
    assert "<redacted>" in redacted and "secret" not in redacted


# ----------------------------------------------------------- host patterns
def test_host_patterns_generalise_rotating_subdomains():
    assert learn_host_pattern("https://url8792.mail.anthropic.com/ls/click") == (
        "url*.mail.anthropic.com"
    )
    assert learn_host_pattern("https://url1234.mail.anthropic.com/x") == (
        "url*.mail.anthropic.com"
    )


def test_host_pattern_of_a_plain_host_is_the_host():
    assert learn_host_pattern("https://links.acme-campaign.net/confirm/click") == (
        "links.acme-campaign.net"
    )


def test_learned_pattern_filters_without_any_ai_call(db):
    db.add_link_pattern("links.acme-campaign.net", kind="tracker", source="ai")
    learned = db.tracker_patterns()
    urls = extract_link_urls(f"{NOVEL_TRACKER} {MAGIC}", learned=learned)
    assert urls == [MAGIC], "the learned pattern must hide the wrapper offline"


def test_learning_removes_the_wrapper_from_the_list_entirely():
    """The magic link already wins on score; learning is what hides the wrapper."""
    before = [link.url for link in extract_links(f"{NOVEL_TRACKER} {MAGIC}")]
    assert NOVEL_TRACKER in before, "unknown wrappers are still listed at first"

    after = [
        link.url
        for link in extract_links(f"{NOVEL_TRACKER} {MAGIC}", learned=["links.acme-campaign.net"])
    ]
    assert after == [MAGIC]


# ---------------------------------------------------------------- the loop
def build(config, db, mailbox, judge):
    return GmailPoller(
        config,
        db,
        client_factory=lambda host: FakeImap(mailbox),
        judge=judge,
        sleep=lambda seconds: None,
    )


def mail_with_two_links(message_id: str) -> bytes:
    return make_raw_email(
        to=ALIAS,
        subject="Your secure link",
        body=f"Log in: {MAGIC}\n\nOr click: {NOVEL_TRACKER}",
        message_id=message_id,
    )


def test_ambiguous_mail_teaches_a_pattern_and_reorders(config, db):
    db.add_alias(1, "claude")
    calls: list[list[str]] = []

    def judge(urls):
        calls.append(urls)
        # index 1 is the wrapper, index 0 is the real link
        return 0, (1,)

    mailbox = FakeMailbox()
    mailbox.add(1, mail_with_two_links("<m1@x>"))
    build(config, db, mailbox, judge).run_once()

    assert calls, "the judge should have been consulted once"
    # The judge receives redacted values only -- enforced before the call, so no
    # implementation can accidentally forward a token.
    assert "34a2fff8" not in " ".join(calls[0]), "a magic-link token must never leave"
    assert "email=" not in " ".join(calls[0])
    assert all(redact_url(url) == url for url in calls[0]), "already redacted"

    patterns = [p.pattern for p in db.list_link_patterns()]
    assert patterns == ["links.acme-campaign.net"]

    stored = db.recent_messages(1)[0]
    assert stored.links[0] == MAGIC, "the real link must be stored first"


def test_learned_mail_is_not_sent_to_the_model_again(config, db):
    db.add_alias(1, "claude")
    db.add_link_pattern("links.acme-campaign.net", kind="tracker", source="ai")
    calls: list[list[str]] = []

    def judge(urls):
        calls.append(urls)
        return 0, (1,)

    mailbox = FakeMailbox()
    mailbox.add(1, mail_with_two_links("<m1@x>"))
    build(config, db, mailbox, judge).run_once()

    assert calls == [], "a learned pattern must make the question moot"
    assert db.recent_messages(1)[0].links[0] == MAGIC


def test_unambiguous_mail_never_reaches_the_model(config, db):
    db.add_alias(1, "claude")
    calls = []

    def judge(urls):
        calls.append(urls)
        return 0, ()

    mailbox = FakeMailbox()
    mailbox.add(
        1,
        make_raw_email(
            to=ALIAS,
            subject="Verify",
            body="Confirm: https://app.acme.test/confirm?token=abc",
            message_id="<m1@x>",
        ),
    )
    build(config, db, mailbox, judge).run_once()
    assert calls == []


def test_a_failing_judge_never_costs_a_message(config, db):
    """The judge is optional assistance: if it breaks, mail still gets stored."""
    db.add_alias(1, "claude")

    def broken_judge(urls):
        raise RuntimeError("openrouter down")

    mailbox = FakeMailbox()
    mailbox.add(1, mail_with_two_links("<m1@x>"))
    build(config, db, mailbox, broken_judge).run_once()

    assert db.list_link_patterns() == [], "nothing was guessed"
    stored = db.recent_messages(1)
    assert len(stored) == 1, "the message must still arrive"
    assert stored[0].links[0] == MAGIC


def test_judge_disabled_by_default(config, db):
    """No API key configured means no judge, and no calls (this is the default)."""
    from gmailbot.app import build_application

    db.add_alias(1, "claude")
    application = build_application(config)
    assert application.bot_data["poller"]._judge is None
    application.bot_data["db"].close()


def test_judge_calls_are_capped_per_cycle(config, db, monkeypatch):
    """A mailbox full of two-link mail must not turn into an API bill."""
    db.add_alias(1, "claude")
    calls: list[list[str]] = []

    def judge(urls):
        calls.append(urls)
        return 0, ()

    mailbox = FakeMailbox()
    for uid in range(1, 9):
        mailbox.add(uid, mail_with_two_links(f"<m{uid}@x>"))
    build(config, db, mailbox, judge).run_once()

    assert len(calls) == 5, "MAX_JUDGE_CALLS_PER_CYCLE must bound the spend"
    assert db.stats()["messages"] == 8, "every message is still stored"


# ------------------------------------------------------------- admin control
def test_bad_pattern_can_be_removed_by_the_admin(config, db):
    from gmailbot.handlers import BotHandlers
    from gmailbot.membership import MembershipGate
    from gmailbot.notify import Notifier
    from gmailbot.otp import OtpExtractor
    from gmailbot.aliases import AliasGenerator
    from tests.fakes import FakeContext, FakeMessage, FakeQuery, FakeUpdate, FakeUser

    db.add_link_pattern("links.acme-campaign.net", kind="tracker", source="ai")
    handlers = BotHandlers(
        config, db,
        generator=AliasGenerator(),
        extractor=OtpExtractor(),
        notifier=Notifier(lambda stored: None),
        gate=MembershipGate(db, enabled=False),
    )
    admin = config.admin_user_id

    message = FakeMessage(text="/trackers")
    update = FakeUpdate(user=FakeUser(admin), message=message)
    run(handlers.trackers(update, FakeContext(bot=FakeBot())))
    assert "links.acme-campaign.net" in message.last.text

    query = FakeQuery(data="ad:rmtrk:0", user=FakeUser(admin), message=FakeMessage(text="p"))
    update2 = FakeUpdate(user=FakeUser(admin), message=query.message, query=query)
    run(handlers.on_callback(update2, FakeContext(bot=FakeBot())))
    assert db.list_link_patterns() == [], "the admin must be able to undo a bad call"


def test_trackers_command_is_admin_only(config, db):
    from gmailbot.aliases import AliasGenerator
    from gmailbot.handlers import BotHandlers
    from gmailbot.membership import MembershipGate
    from gmailbot.notify import Notifier
    from gmailbot.otp import OtpExtractor
    from tests.fakes import FakeContext, FakeMessage, FakeUpdate, FakeUser

    handlers = BotHandlers(
        config, db,
        generator=AliasGenerator(),
        extractor=OtpExtractor(),
        notifier=Notifier(lambda stored: None),
        gate=MembershipGate(db, enabled=False),
    )
    message = FakeMessage(text="/trackers")
    update = FakeUpdate(user=FakeUser(config.admin_user_id + 1), message=message)
    run(handlers.trackers(update, FakeContext(bot=FakeBot())))
    assert message.sent == []
