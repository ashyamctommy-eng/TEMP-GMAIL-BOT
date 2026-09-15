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
    FakeChat,
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


def make_handlers(config, db, *, gate=None, **overrides):
    """Construct a fresh handler set with overridden config values."""
    tuned = replace(config, **overrides)
    return BotHandlers(
        tuned,
        db,
        generator=AliasGenerator(),
        extractor=OtpExtractor(),
        notifier=Notifier(lambda stored: None),
        guard=ChannelGuard(tuned.feedback_channel_id),
        gate=gate,
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


# ------------------------------------------------------------- admin panel
def panel_buttons(markup):
    return [(button.text, button.style, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_admin_panel_is_admin_only(handlers, bot, db):
    update, context = make_command_update("admin", user_id=7, bot=bot)
    run(handlers.admin(update, context))
    assert "Admin only" in update.effective_message.last.text
    assert update.effective_message.last.markup is None


def test_admin_panel_has_coloured_buttons_for_every_action(handlers, bot, db):
    update, context = make_command_update("admin", user_id=ADMIN, bot=bot)
    run(handlers.admin(update, context))
    text = update.effective_message.last.text
    buttons = panel_buttons(update.effective_message.last.markup)

    assert "Admin panel" in text
    callbacks = {data for _, _, data in buttons}
    assert {
        "ad:stats", "ad:bc", "ad:ban", "ad:unban",
        "ad:addch", "ad:delch", "ad:chs", "ad:panel", "ad:close",
    } <= callbacks
    styles = {style for _, style, _ in buttons}
    assert {"primary", "success", "danger"} <= {str(s.value if hasattr(s, "value") else s) for s in styles}
    # dangerous actions really are the red ones
    red = {data for _, style, data in buttons if str(getattr(style, "value", style)) == "danger"}
    assert {"ad:ban", "ad:delch", "ad:close"} <= red


def test_panel_callback_from_a_non_admin_is_refused(handlers, bot, db):
    update, context, query = make_callback_update("ad:stats", user_id=7, bot=bot)
    run(handlers.on_callback(update, context))
    assert query.answers == ["Admin only."]
    assert query.edits == []


def test_panel_stats_callback_edits_in_place(handlers, bot, db):
    db.add_alias(1, "tiger123")
    update, context, query = make_callback_update("ad:stats", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert query.edits and "Aliases: 1" in query.edits[-1].text


def test_panel_ban_flow_asks_then_applies(handlers, bot, db):
    update, context, query = make_callback_update("ad:ban", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert context.user_data["pending_admin"] == "ban"
    assert "user id" in query.edits[-1].text.lower()

    update2, context2 = make_text_update("555", user_id=ADMIN, bot=bot, user_data=context.user_data)
    run(handlers.on_text(update2, context2))
    assert db.is_banned(555)
    assert "pending_admin" not in context2.user_data


def test_panel_ban_flow_rejects_junk(handlers, bot, db):
    update, context, _ = make_callback_update("ad:ban", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    update2, context2 = make_text_update("not-a-number", user_id=ADMIN, bot=bot, user_data=context.user_data)
    run(handlers.on_text(update2, context2))
    assert "not a numeric user id" in update2.effective_message.last.text


def test_panel_broadcast_flow_sends_to_everyone(handlers, bot, db):
    for user_id in (1, 2, 3):
        db.ensure_user(user_id)
    update, context, _ = make_callback_update("ad:bc", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert context.user_data["pending_admin"] == "broadcast"

    update2, context2 = make_text_update("Server maintenance tonight", user_id=ADMIN, bot=bot, user_data=context.user_data)
    run(handlers.on_text(update2, context2))

    recipients = {chat_id for chat_id, _, _ in bot.messages}
    # the admin is a registered user too (ensure_user on first interaction), so
    # they receive the broadcast as well as 1, 2 and 3.
    assert {1, 2, 3} <= recipients
    assert all(text == "Server maintenance tonight" for _, text, _ in bot.messages)
    assert f"{len(recipients)} delivered" in update2.effective_message.last.text


def test_panel_add_channel_flow(handlers, bot, db):
    bot.chats["@nativecodes"] = FakeChat(-1001, username="nativecodes", title="Native Codes")
    update, context, _ = make_callback_update("ad:addch", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert context.user_data["pending_admin"] == "addchannel"

    update2, context2 = make_text_update("@nativecodes", user_id=ADMIN, bot=bot, user_data=context.user_data)
    run(handlers.on_text(update2, context2))
    channels = db.list_required_channels()
    assert [c.chat_id for c in channels] == ["@nativecodes"]
    assert channels[0].invite_link == "https://t.me/nativecodes"
    assert "now required" in update2.effective_message.last.text


def test_cancel_clears_a_pending_admin_action(handlers, bot, db):
    update, context, _ = make_callback_update("ad:ban", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    update2, context2 = make_command_update("cancel", user_id=ADMIN, bot=bot, user_data=context.user_data)
    run(handlers.cancel(update2, context2))
    assert "pending_admin" not in context2.user_data
    assert "Cancelled" in update2.effective_message.last.text


def test_panel_close_deletes_the_message(handlers, bot, db):
    update, context, query = make_callback_update("ad:close", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert query.message.deleted is True


# ------------------------------------------------------- channel management
def test_channels_command_lists_with_remove_buttons(handlers, bot, db):
    db.add_required_channel("@nativecodes", "Native Codes", "https://t.me/nativecodes")
    db.add_required_channel("-100999", "Private", None)
    update, context = make_command_update("channels", user_id=ADMIN, bot=bot)
    run(handlers.channels(update, context))
    text = update.effective_message.last.text
    assert "1." in text and "Native Codes" in text and "2." in text and "Private" in text
    data = [b.callback_data for row in update.effective_message.last.markup.inline_keyboard for b in row]
    assert "ad:rmch:@nativecodes" in data


def test_remove_channel_button_removes_it(handlers, bot, db):
    db.add_required_channel("@nativecodes", "Native Codes", None)
    update, context, query = make_callback_update("ad:rmch:@nativecodes", user_id=ADMIN, bot=bot)
    run(handlers.on_callback(update, context))
    assert db.list_required_channels() == []
    assert "None yet" in query.edits[-1].text


def test_addchannel_command_stores_the_channel(handlers, bot, db):
    bot.chats["@nativecodes"] = FakeChat(-1001, username="nativecodes", title="Native Codes")
    update, context = make_command_update("addchannel", ["@nativecodes"], user_id=ADMIN, bot=bot)
    run(handlers.addchannel(update, context))
    assert db.list_required_channels()[0].chat_id == "@nativecodes"


def test_addchannel_warns_when_the_bot_is_not_admin(handlers, bot, db):
    bot.chats["@nativecodes"] = FakeChat(-1001, username="nativecodes", title="Native Codes")
    bot.member_status[bot.id] = "left"
    update, context = make_command_update("addchannel", ["@nativecodes"], user_id=ADMIN, bot=bot)
    run(handlers.addchannel(update, context))
    assert "not an admin" in update.effective_message.last.text


def test_addchannel_usage_and_admin_only(handlers, bot, db):
    update, context = make_command_update("addchannel", user_id=ADMIN, bot=bot)
    run(handlers.addchannel(update, context))
    assert "Usage" in update.effective_message.last.text

    update2, context2 = make_command_update("addchannel", ["@x"], user_id=7, bot=bot)
    run(handlers.addchannel(update2, context2))
    assert update2.effective_message.sent == []


def test_delchannel_by_handle_and_by_index(handlers, bot, db):
    db.add_required_channel("@one", "One", None)
    db.add_required_channel("@two", "Two", None)

    update, context = make_command_update("delchannel", ["2"], user_id=ADMIN, bot=bot)
    run(handlers.delchannel(update, context))
    assert [c.chat_id for c in db.list_required_channels()] == ["@one"]

    update2, context2 = make_command_update("delchannel", ["@one"], user_id=ADMIN, bot=bot)
    run(handlers.delchannel(update2, context2))
    assert db.list_required_channels() == []


def test_delchannel_reports_an_unknown_channel(handlers, bot, db):
    update, context = make_command_update("delchannel", ["@nope"], user_id=ADMIN, bot=bot)
    run(handlers.delchannel(update, context))
    assert "No required channel matches" in update.effective_message.last.text


# --------------------------------------------------------------- verification
def test_verify_button_unlocks_the_bot(handlers, bot, db):
    db.add_required_channel("@nativecodes", "Native Codes", None)
    bot.default_member_status = "left"
    update, context, query = make_callback_update("jv", bot=bot, user_id=7)
    run(handlers.on_callback(update, context))
    assert "Still not a member" in str(query.answers[0])

    bot.member_status[7] = "member"
    update2, context2, query2 = make_callback_update("jv", bot=bot, user_id=7)
    run(handlers.on_callback(update2, context2))
    assert "verified" in str(query2.answers[0]).lower()
    assert query2.edits and "TempGail" or "𝑻𝒆𝒎𝒑" in query2.edits[-1].text
