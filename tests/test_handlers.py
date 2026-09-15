"""Handler behaviour: what users actually experience."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from gmailbot.aliases import AliasGenerator
from gmailbot.handlers import BotHandlers, ChannelGuard
from gmailbot.notify import Notifier
from gmailbot.otp import OtpExtractor
from tests.fakes import (
    FakeBot,
    FakeChatMember,
    FakeContext,
    FakeMessage,
    FakePhoto,
    FakeQuery,
    FakeUpdate,
    FakeUser,
)

ADMIN = 42


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def handlers(config, db, generator, extractor, notifier) -> BotHandlers:
    return BotHandlers(
        config,
        db,
        generator=generator,
        extractor=extractor,
        notifier=notifier,
        guard=ChannelGuard(config.feedback_channel_id),
    )


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot(FakeChatMember("administrator"))


def make_text_update(text: str, *, user_id: int = 7, bot: FakeBot | None = None, user_data: dict | None = None):
    message = FakeMessage(text=text)
    update = FakeUpdate(user=FakeUser(user_id), message=message)
    context = FakeContext(bot=bot, user_data=user_data)
    return update, context


def make_command_update(command: str, args: list[str] | None = None, *, user_id: int = 7, bot: FakeBot | None = None, user_data: dict | None = None):
    message = FakeMessage(text=f"/{command}")
    update = FakeUpdate(user=FakeUser(user_id), message=message)
    context = FakeContext(bot=bot, args=args or [], user_data=user_data)
    return update, context


def make_callback_update(data: str, *, user_id: int = 7, bot: FakeBot | None = None, user_data: dict | None = None):
    message = FakeMessage(text="previous")
    query = FakeQuery(data=data, user=FakeUser(user_id), message=message)
    update = FakeUpdate(user=FakeUser(user_id), message=message, query=query)
    context = FakeContext(bot=bot, user_data=user_data)
    return update, context, query


# ------------------------------------------------------------------- basics
def test_start_shows_welcome_and_menu(handlers, bot, config):
    update, context = make_command_update("start", bot=bot)
    run(handlers.start(update, context))
    last = update.effective_message.last
    assert config.brand_name in last.text
    assert last.parse_mode == "HTML"
    assert last.markup is not None
    assert config.credit_line in last.text


def test_start_attaches_the_brand_photo(handlers, bot, config):
    update, context = make_command_update("start", bot=bot)
    run(handlers.start(update, context))
    last = update.effective_message.last
    assert last.photo and last.photo.endswith("tempgmail.jpg")
    # The caption carries the whole message: nothing is dropped for the image.
    assert config.brand_name in last.text and config.credit_line in last.text


def test_otp_command_attaches_the_brand_photo(handlers, bot, db, config):
    db.add_alias(7, "tiger123")
    db.add_message("tiger123", "Your code", "Your code is 483920", otp="483920")
    update, context = make_command_update("otp", bot=bot)
    run(handlers.otp(update, context))
    assert update.effective_message.last.photo
    assert "<code>483920</code>" in update.effective_message.last.text


def test_callback_edits_do_not_try_to_attach_a_photo(handlers, bot, db):
    """Telegram cannot turn a text message into a photo by editing it."""
    db.add_alias(7, "tiger123")
    db.add_message("tiger123", "Your code", "code 483920", otp="483920")
    update, context, query = make_callback_update("a:otp:0", bot=bot)
    run(handlers.on_callback(update, context))
    assert query.edits and all(edit.photo is None for edit in query.edits)


def test_photo_can_be_switched_off(config, db, bot):
    off = make_handlers(config, db, send_brand_photo=False)
    update, context = make_command_update("start", bot=bot)
    run(off.start(update, context))
    assert update.effective_message.last.photo is None
    assert "TempGail" or config.brand_name in update.effective_message.last.text


def test_every_user_facing_message_carries_the_credit_footer(handlers, bot, db, config):
    db.add_alias(7, "tiger123")
    db.add_message("tiger123", "Subject", "Body with 483920 code", otp="483920", links=["https://a.test/v?token=1"])
    steps = [
        ("start", [], None),
        ("generate", [], None),
        ("history", [], None),
        ("otp", [], None),
        ("view", ["tiger123"], None),
        ("delete", ["tiger123"], None),
        ("feedback", [], None),
        ("on_text", [], "hello there"),
    ]
    checked = 0
    for name, args, typed in steps:
        update, context = make_command_update(name, args, bot=bot) if typed is None else make_text_update(typed, bot=bot)
        run(getattr(handlers, name)(update, context))
        for sent in update.effective_message.sent:
            assert config.credit_line in sent.text, f"{name} reply is missing the footer"
            assert sent.text.count(config.credit_line) == 1, f"{name} footer duplicated"
            checked += 1
    assert checked >= len(steps)


def test_long_otp_message_becomes_caption_plus_full_message(handlers, bot, db, config):
    """A caption is capped at 1024 units, so the code must ride in the text part."""
    db.add_alias(7, "tiger123")
    for index in range(5):
        db.add_message(
            "tiger123",
            f"Verification message {index} " + "x" * 80,
            "padding " * 40 + f"code {700000 + index}",
            otp=str(700000 + index),
        )
    update, context = make_command_update("otp", bot=bot)
    run(handlers.otp(update, context))

    photo_sends = [s for s in update.effective_message.sent if s.photo]
    text_sends = [s for s in update.effective_message.sent if not s.photo]
    assert photo_sends, "expected the photo"
    assert text_sends, "expected the full message after the photo"
    # The overflow message still contains the codes and the footer.
    assert any("<code>700004</code>" in sent.text for sent in text_sends)
    assert all(config.credit_line in sent.text for sent in text_sends)


def test_every_reply_uses_html_parse_mode(handlers, bot, db):
    """The original re-sent unformatted text whenever Markdown failed."""
    db.add_alias(1, "tiger123")
    for update, context in [
        make_command_update("start", bot=bot),
        make_command_update("generate", bot=bot),
        make_command_update("history", bot=bot),
        make_command_update("otp", bot=bot),
    ]:
        run(getattr(handlers, update.effective_message.text.lstrip("/"))(update, context))
        assert update.effective_message.last.parse_mode == "HTML"


def test_banned_user_is_refused(handlers, bot, db):
    db.set_banned(7, True)
    update, context = make_command_update("generate", bot=bot)
    run(handlers.generate(update, context))
    assert "banned" in update.effective_message.last.text
    assert db.stats()["aliases"] == 0


# -------------------------------------------------------------- generation
def test_generate_creates_an_alias_and_registers_the_user(handlers, bot, db):
    update, context = make_command_update("generate", bot=bot)
    run(handlers.generate(update, context))
    assert db.stats()["aliases"] == 1
    assert db.stats()["users"] == 1
    assert "owner+" in update.effective_message.last.text


def test_generate_with_custom_name(handlers, bot, db):
    update, context = make_command_update("generate", ["My-Alias"], bot=bot)
    run(handlers.generate(update, context))
    assert db.alias_owner("my-alias") == 7


def test_generate_rejects_taken_name(handlers, bot, db):
    db.add_alias(1, "popular")
    update, context = make_command_update("generate", ["popular"], bot=bot)
    run(handlers.generate(update, context))
    assert "taken" in update.effective_message.last.text
    assert db.alias_owner("popular") == 1


def make_handlers(config, db, **overrides):
    """Construct a fresh handler set with overridden config values."""
    tuned = replace(config, **overrides)
    return BotHandlers(
        tuned,
        db,
        generator=AliasGenerator(),
        extractor=OtpExtractor(),
        notifier=Notifier(lambda stored: None),
        guard=ChannelGuard(tuned.feedback_channel_id),
    )


def test_alias_limit_is_enforced(config, bot, db):
    limited = make_handlers(config, db, max_aliases_per_user=1)
    update, context = make_command_update("generate", bot=bot)
    run(limited.generate(update, context))
    assert db.stats()["aliases"] == 1

    update2, context2 = make_command_update("generate", bot=bot)
    run(limited.generate(update2, context2))
    assert db.stats()["aliases"] == 1
    assert "limit" in update2.effective_message.last.text.lower()


def test_generate_is_rate_limited(config, bot, db):
    limited = make_handlers(config, db, generate_per_hour=1)
    run(limited.generate(*make_command_update("generate", bot=bot)))
    update, context = make_command_update("generate", bot=bot)
    run(limited.generate(update, context))
    assert db.stats()["aliases"] == 1
    assert "Slow down" in update.effective_message.last.text


# --------------------------------------------------------------- free text
def test_greeting_does_not_create_an_alias(handlers, bot, db):
    """'hi' used to become an alias named 'hi'."""
    update, context = make_text_update("hi", bot=bot)
    run(handlers.on_text(update, context))
    assert db.stats()["aliases"] == 0
    assert "didn't catch" in update.effective_message.last.text


def test_plain_word_asks_before_creating(handlers, bot, db):
    update, context = make_text_update("myname", bot=bot)
    run(handlers.on_text(update, context))
    assert db.stats()["aliases"] == 0
    markup = update.effective_message.last.markup
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "c:myname" in data


def test_confirmed_creation_creates_the_alias(handlers, bot, db):
    update, context, query = make_callback_update("c:myname", bot=bot)
    run(handlers.on_callback(update, context))
    assert db.alias_owner("myname") == 7
    assert "myname" in query.edits[-1].text


def test_reserved_word_is_rejected(handlers, bot, db):
    update, context = make_command_update("generate", ["otp"], bot=bot)
    run(handlers.generate(update, context))
    assert db.stats()["aliases"] == 0
    assert "reserved" in update.effective_message.last.text


# --------------------------------------------------------------- feedback
def test_feedback_flow_escapes_and_delivers(handlers, bot, db):
    update, context = make_command_update("feedback", bot=bot)
    run(handlers.feedback(update, context))
    assert context.user_data["mode"] == "feedback"

    update2, context2 = make_text_update("<b>forged header</b> hello", bot=bot, user_data=context.user_data)
    run(handlers.on_text(update2, context2))

    channel_id, text, parse_mode = bot.messages[-1]
    assert channel_id == handlers.config.feedback_channel_id
    assert parse_mode == "HTML"
    assert "&lt;b&gt;forged header&lt;/b&gt;" in text
    assert "<b>forged header</b>" not in text
    assert "Feedback ID" in text
    assert db.stats()["feedback"] == 1
    assert "mode" not in context2.user_data


def test_feedback_permission_probe_is_cached(handlers, bot):
    """The original sent and deleted a test message on every /feedback press."""
    for _ in range(3):
        update, context = make_command_update("feedback", bot=bot)
        run(handlers.feedback(update, context))
    assert bot.get_chat_member_calls == 1
    assert bot.deleted == []


def test_feedback_degrades_when_bot_lacks_permission(handlers, db):
    bot = FakeBot(FakeChatMember("restricted", can_send_messages=False))
    update, context = make_command_update("feedback", bot=bot)
    run(handlers.feedback(update, context))
    assert "unavailable" in update.effective_message.last.text


def test_photo_outside_feedback_mode_is_explained(handlers, bot, db):
    message = FakeMessage(photo=[FakePhoto()], caption="look")
    update = FakeUpdate(user=FakeUser(7), message=message)
    context = FakeContext(bot=bot)
    run(handlers.on_photo(update, context))
    assert "feedback" in update.effective_message.last.text.lower()
    assert bot.photos == []


def test_photo_feedback_is_forwarded_with_identity(handlers, bot, db):
    update, context = make_command_update("feedback", bot=bot)
    run(handlers.feedback(update, context))
    message = FakeMessage(photo=[FakePhoto("small", 10), FakePhoto("large", 5000)], caption="bug")
    photo_update = FakeUpdate(user=FakeUser(7), message=message)
    run(handlers.on_photo(photo_update, context))
    assert bot.photos and bot.photos[0][1] == "large"  # largest rendition


# ------------------------------------------------------------------ viewing
def test_otp_list_shows_codes_in_code_tags(handlers, bot, db):
    db.add_alias(7, "tiger123")
    db.add_message("tiger123", "Your code", "Your code is 483920", otp="483920")
    update, context = make_command_update("otp", bot=bot)
    run(handlers.otp(update, context))
    assert "<code>483920</code>" in update.effective_message.last.text


def test_view_rejects_foreign_alias(handlers, bot, db):
    db.add_alias(1, "tiger123")
    update, context = make_command_update("view", ["tiger123"], bot=bot)
    run(handlers.view(update, context))
    assert "No active alias" in update.effective_message.last.text


def test_view_renders_owned_messages(handlers, bot, db):
    db.add_alias(7, "tiger123")
    db.add_message("tiger123", "Welcome", "hi there", otp="111222")
    update, context = make_command_update("view", ["tiger123"], bot=bot)
    run(handlers.view(update, context))
    text = update.effective_message.last.text
    assert "tiger123" in text and "<code>111222</code>" in text


def test_reveal_secret_refuses_other_users_messages(handlers, bot, db):
    db.add_alias(1, "tiger123")
    stored = db.add_message("tiger123", "Subject", "Body", otp="999999", links=["https://x.test/v"])
    update, context, query = make_callback_update(f"s:{stored.message_id}:link", user_id=7, bot=bot)
    run(handlers.on_callback(update, context))
    payloads = [edit.text for edit in query.edits] + [m.text for m in update.effective_message.sent]
    assert any("no longer available" in payload for payload in payloads)
    assert not any("999999" in payload for payload in payloads)


def test_reveal_secret_sends_full_value(handlers, bot, db):
    url = "https://accounts.example.com/verify?token=" + "c" * 80
    db.add_alias(7, "tiger123")
    stored = db.add_message("tiger123", "Subject", "Body", links=[url])
    update, context, query = make_callback_update(f"s:{stored.message_id}:link", bot=bot)
    run(handlers.on_callback(update, context))
    payloads = [edit.text for edit in query.edits] + [m.text for m in update.effective_message.sent]
    assert any(url in payload for payload in payloads)


def test_delete_then_restore(handlers, bot, db):
    db.add_alias(7, "tiger123")
    update, context = make_command_update("delete", ["tiger123"], bot=bot)
    run(handlers.delete(update, context))
    assert db.owned_alias(7, "tiger123") is None

    update2, context2, query = make_callback_update("r:tiger123", bot=bot)
    run(handlers.on_callback(update2, context2))
    assert db.owned_alias(7, "tiger123") is not None


def test_unknown_callback_is_answered_not_crashed(handlers, bot):
    update, context, query = make_callback_update("total:garbage:data", bot=bot)
    run(handlers.on_callback(update, context))
    assert query.answers, "every callback must be answered"


# -------------------------------------------------------------------- admin
def test_admin_commands_are_admin_only(handlers, bot, db):
    db.ensure_user(7)
    for command, args in [("stats", []), ("ban", ["1"]), ("broadcast", ["hi"]), ("unban", ["1"])]:
        update, context = make_command_update(command, args, user_id=7, bot=bot)
        run(getattr(handlers, command)(update, context))
        assert update.effective_message.sent == []


def test_admin_stats(handlers, bot, db):
    db.add_alias(1, "tiger123")
    update, context = make_command_update("stats", user_id=ADMIN, bot=bot)
    run(handlers.stats(update, context))
    assert "Aliases: 1" in update.effective_message.last.text


def test_broadcast_reports_delivery(handlers, bot, db):
    for user_id in (1, 2, 3):
        db.ensure_user(user_id)
    bot.fail_for = {2}
    update, context = make_command_update("broadcast", ["hello"], user_id=ADMIN, bot=bot)
    run(handlers.broadcast(update, context))
    assert "2 delivered" in update.effective_message.last.text
    assert "1 failed" in update.effective_message.last.text


def test_ban_and_unban(handlers, bot, db):
    run(handlers.ban(*make_command_update("ban", ["9"], user_id=ADMIN, bot=bot)))
    assert db.is_banned(9)
    run(handlers.unban(*make_command_update("unban", ["9"], user_id=ADMIN, bot=bot)))
    assert not db.is_banned(9)


# ------------------------------------------------------------------- errors
def test_error_handler_does_not_raise(handlers, bot, db):
    update, context = make_text_update("x", bot=bot)
    context.error = RuntimeError("boom")
    run(handlers.on_error(update, context))
    assert "Something went wrong" in update.effective_message.last.text
