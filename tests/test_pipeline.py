"""End-to-end: mail arrives in the mailbox and a Telegram alert is produced."""

from __future__ import annotations

import asyncio

from gmailbot import formatting as fmt
from gmailbot.handlers import build_notification
from gmailbot.mail import GmailPoller
from gmailbot.models import utcnow
from gmailbot.notify import Notifier
from tests.fakes import FakeBot, FakeMailbox, FakeImap, make_raw_email

ALIAS_ADDRESS = "owner+tiger123@gmail.com"


def test_mail_to_notification_pipeline(config, db):
    """Gmail bytes in, rendered Telegram alert out -- without a network."""
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(
        1,
        make_raw_email(
            to=ALIAS_ADDRESS,
            subject="Verify your Acme account",
            body="Your verification code is 483920.\n\nOr click https://acme.test/verify?token=zz",
            message_id="<otp-1@acme>",
        ),
    )
    client = FakeImap(mailbox)
    bot = FakeBot()

    async def scenario() -> list[tuple[int, str, object]]:
        loop = asyncio.get_running_loop()

        class FakeApplication:
            def __init__(self) -> None:
                self.bot = bot

        application = FakeApplication()
        notifier = Notifier(build_notification(config))
        notifier.bind(loop, application)
        notifier.start()

        poller = GmailPoller(
            config,
            db,
            on_message=notifier.emit,
            client_factory=lambda host: client,
            sleep=lambda seconds: None,
        )
        poller.run_once()
        await asyncio.sleep(0.05)  # let the notifier task drain
        await notifier.stop()
        return bot.messages

    asyncio.run(scenario())
    # OTP alerts go out as a photo with the whole alert as the caption, so the
    # code and links must survive that switch.
    assert len(bot.photo_calls) == 1
    call = bot.photo_calls[0]
    assert call["chat_id"] == 1
    assert call["photo"].endswith("tempgmail.jpg")
    caption = fmt.unstyle(call["caption"])
    assert "<code>483920</code>" in caption
    assert "https://acme.test/verify?token=zz" in caption
    assert "New code" in caption
    assert config.credit_line in caption, "alerts are branded too"
    assert bot.messages == [], "caption fitted, so no second message is needed"


def test_plain_mail_produces_a_quiet_notification(config, db):
    db.add_alias(1, "tiger123")
    mailbox = FakeMailbox()
    mailbox.add(
        1,
        make_raw_email(to=ALIAS_ADDRESS, subject="Newsletter", body="nothing numeric", message_id="<n@x>"),
    )
    bot = FakeBot()

    async def scenario():
        class FakeApplication:
            def __init__(self) -> None:
                self.bot = bot

        notifier = Notifier(build_notification(config))
        notifier.bind(asyncio.get_running_loop(), FakeApplication())
        notifier.start()
        GmailPoller(
            config, db, on_message=notifier.emit, client_factory=lambda host: FakeImap(mailbox),
            sleep=lambda seconds: None,
        ).run_once()
        await asyncio.sleep(0.05)
        await notifier.stop()

    asyncio.run(scenario())
    assert len(bot.messages) == 1
    assert "New email" in fmt.unstyle(bot.messages[0][1])
    assert config.credit_line in bot.messages[0][1]
    assert "483920" not in bot.messages[0][1]
    # Plain mail stays text-only; only code alerts get the image.
    assert bot.photo_calls == []


def test_notifier_drops_blocked_users_without_retrying_forever(config):
    """Forbidden must not be retried: the original retried every error."""
    from telegram.error import Forbidden

    attempts = {"count": 0}

    class BlockedBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            attempts["count"] += 1
            raise Forbidden("user blocked the bot")

        async def send_photo(self, chat_id, photo, caption=None, **kwargs):
            attempts["count"] += 1
            raise Forbidden("user blocked the bot")

    async def scenario():
        class FakeApplication:
            def __init__(self) -> None:
                self.bot = BlockedBot()

        notifier = Notifier(build_notification(config))
        notifier.bind(asyncio.get_running_loop(), FakeApplication())
        notifier.start()
        from gmailbot.models import StoredMessage

        stored = StoredMessage(
            user_id=5, message_id=1, alias="a", subject="s", otp="1234",
            links=[], received_at=utcnow(),
        )
        notifier.emit(stored)
        notifier.emit(stored)
        await asyncio.sleep(0.05)
        await notifier.stop()

    asyncio.run(scenario())
    assert attempts["count"] == 1, "Forbidden must not be retried"


def test_notifier_buffers_until_ready(config):
    from gmailbot.models import StoredMessage

    notifier = Notifier(build_notification(config))
    stored = StoredMessage(
            user_id=5, message_id=1, alias="a", subject="s", otp="1234",
            links=[], received_at=utcnow(),
        )
    notifier.emit(stored)  # before bind(): must be buffered, not dropped
    assert len(notifier._buffered) == 1


def test_link_only_mail_is_headlined_as_a_link_not_a_code(config, db):
    db.add_alias(1, "claude")
    mailbox = FakeMailbox()
    mailbox.add(
        1,
        make_raw_email(
            to="owner+claude@gmail.com",
            subject="Your secure link to Claude.ai is here",
            body="Log in: https://claude.ai/magic-link#tok",
            message_id="<magic@claude.ai>",
        ),
    )
    bot = FakeBot()

    async def scenario():
        class FakeApplication:
            def __init__(self) -> None:
                self.bot = bot

        notifier = Notifier(build_notification(config))
        notifier.bind(asyncio.get_running_loop(), FakeApplication())
        notifier.start()
        GmailPoller(
            config, db, on_message=notifier.emit,
            client_factory=lambda host: FakeImap(mailbox), sleep=lambda seconds: None,
        ).run_once()
        await asyncio.sleep(0.05)
        await notifier.stop()

    asyncio.run(scenario())
    raw = bot.photo_calls[0]["caption"] if bot.photo_calls else bot.messages[0][1]
    text = fmt.unstyle(raw)
    assert "New link" in text and "New code" not in text
    assert "https://claude.ai/magic-link#tok" in text
