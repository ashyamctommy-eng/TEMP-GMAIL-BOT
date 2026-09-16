"""Force-join gate: membership checks, caching, fail-open behaviour."""

from __future__ import annotations

import asyncio

import pytest
from telegram.ext import ApplicationHandlerStop

from gmailbot import callbacks as cb
from gmailbot.membership import MembershipGate, normalize_channel_ref
from tests.fakes import FakeBot, FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, FakeUser

CHANNEL = "@nativecodes"
USER = 7
ADMIN = 42


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gate(db, config) -> MembershipGate:
    return MembershipGate(db, enabled=True, cache_seconds=300, admin_user_id=ADMIN)


@pytest.fixture
def bot() -> FakeBot:
    fake = FakeBot()
    fake.chats[CHANNEL] = FakeChat(-1001, username="nativecodes", title="Native Codes")
    fake.default_member_status = "left"  # nobody is a member unless the test says so
    return fake


def text_update(text: str = "/start", *, user_id: int = USER, bot: FakeBot | None = None):
    message = FakeMessage(text=text)
    update = FakeUpdate(user=FakeUser(user_id), message=message)
    return update, FakeContext(bot=bot), message


def callback_update(data: str, *, user_id: int = USER, bot: FakeBot | None = None):
    message = FakeMessage(text="panel")
    query = FakeQuery(data=data, user=FakeUser(user_id), message=message)
    return FakeUpdate(user=FakeUser(user_id), message=message, query=query), FakeContext(bot=bot), query


# ------------------------------------------------------------------- the gate
def test_non_member_is_prompted_and_stopped(gate, bot, db):
    db.add_required_channel(CHANNEL, "Native Codes", "https://t.me/nativecodes")

    update, context, message = text_update(bot=bot)
    with pytest.raises(ApplicationHandlerStop):
        run(gate.middleware(update, context))

    from gmailbot import formatting as fmt

    text = fmt.unstyle(message.last.text)
    assert "Join required" in text and "Native Codes" in text
    buttons = [b for row in message.last.markup.inline_keyboard for b in row]
    assert any(b.url == "https://t.me/nativecodes" for b in buttons)
    assert any(b.callback_data == cb.JOIN_VERIFY for b in buttons)


def test_member_passes_without_a_message(gate, bot, db):
    db.add_required_channel(CHANNEL, "Native Codes", "https://t.me/nativecodes")
    bot.member_status[USER] = "member"

    update, context, message = text_update(bot=bot)
    run(gate.middleware(update, context))  # must not raise
    assert message.sent == []


@pytest.mark.parametrize("status", ["creator", "administrator", "member", "restricted"])
def test_all_member_states_count_as_joined(gate, bot, db, status):
    db.add_required_channel(CHANNEL, "Native Codes", None)
    bot.member_status[USER] = status
    update, context, _ = text_update(bot=bot)
    run(gate.middleware(update, context))
    assert run(gate.missing(bot, USER)) == []


@pytest.mark.parametrize("status", ["left", "kicked"])
def test_left_and_kicked_are_blocked(gate, bot, db, status):
    db.add_required_channel(CHANNEL, "Native Codes", None)
    bot.member_status[USER] = status
    update, context, _ = text_update(bot=bot)
    with pytest.raises(ApplicationHandlerStop):
        run(gate.middleware(update, context))


def test_admin_never_gets_locked_out(gate, bot, db):
    db.add_required_channel(CHANNEL, "Native Codes", None)

    update, context, message = text_update(user_id=ADMIN, bot=bot)
    run(gate.middleware(update, context))  # must not raise
    assert message.sent == []


def test_verify_button_always_reaches_its_handler(gate, bot, db):
    db.add_required_channel(CHANNEL, "Native Codes", None)

    update, context, query = callback_update(cb.JOIN_VERIFY, bot=bot)
    run(gate.middleware(update, context))  # must not raise, else verify loops forever
    assert query.answers == []


def test_disabled_gate_allows_everyone(db, bot):
    db.add_required_channel(CHANNEL, "Native Codes", None)
    off = MembershipGate(db, enabled=False, admin_user_id=ADMIN)
    update, context, message = text_update(bot=bot)
    run(off.middleware(update, context))
    assert message.sent == []


def test_no_channels_configured_allows_everyone(gate, bot):
    update, context, message = text_update(bot=bot)
    run(gate.middleware(update, context))
    assert message.sent == []


# ----------------------------------------------------------------- caching
def test_membership_is_cached_to_protect_the_rate_limit(gate, bot, db):
    db.add_required_channel(CHANNEL, "Native Codes", None)
    bot.member_status[USER] = "member"

    run(gate.missing(bot, USER))
    run(gate.missing(bot, USER))
    run(gate.missing(bot, USER))
    assert len(bot.membership_calls) == 1

    gate.invalidate(USER)
    run(gate.missing(bot, USER))
    assert len(bot.membership_calls) == 2


def test_verify_invalidation_rechecks_immediately(gate, bot, db):
    """After tapping 'I joined', the answer must not come from a stale cache."""
    db.add_required_channel(CHANNEL, "Native Codes", None)
    assert run(gate.missing(bot, USER)) != []  # cached as "not a member"

    bot.member_status[USER] = "member"
    assert run(gate.missing(bot, USER)) != []  # still cached

    gate.invalidate(USER)
    assert run(gate.missing(bot, USER)) == []


# --------------------------------------------------------------- fail open
def test_unreadable_channel_fails_open_with_a_warning(gate, bot, db):
    db.add_required_channel("@gone", "Deleted", None)
    bot.unresolvable.add("@gone")  # e.g. the channel was deleted or the bot was removed
    update, context, message = text_update(bot=bot)
    run(gate.middleware(update, context))  # not blocked
    assert message.sent == []


def test_broken_check_does_not_lock_everyone_out(gate, bot, db):
    db.add_required_channel("@private", "Private", None)
    bot.unresolvable.add("@private")
    assert run(gate.missing(bot, USER)) == []


# ----------------------------------------------------------------- resolve
def test_resolve_derives_a_public_invite_link(gate, bot):
    channel, warning = run(gate.resolve(bot, "@nativecodes"))
    assert channel is not None
    assert channel.chat_id == "@nativecodes"
    assert channel.invite_link == "https://t.me/nativecodes"
    assert warning is None


def test_resolve_accepts_a_tme_link_and_a_bare_handle(gate, bot):
    for raw in ("https://t.me/nativecodes", "t.me/nativecodes", "nativecodes"):
        channel, _ = run(gate.resolve(bot, raw))
        assert channel is not None and channel.chat_id == "@nativecodes"


def test_resolve_uses_the_invite_link_for_private_channels(gate, bot):
    bot.chats["-1001234567890"] = FakeChat(
        -1001234567890, username=None, title="Private", invite_link="https://t.me/+abc"
    )
    channel, warning = run(gate.resolve(bot, "-1001234567890"))
    assert channel is not None
    assert channel.invite_link == "https://t.me/+abc"
    assert warning is None


def test_resolve_warns_when_the_bot_is_not_admin(gate, bot, db):
    bot.member_status[bot.id] = "left"  # bot itself is not an admin there
    channel, warning = run(gate.resolve(bot, "@nativecodes"))
    assert channel is not None
    assert warning and "not an admin" in warning


def test_resolve_rejects_unresolvable_and_non_channels(gate, bot):
    assert run(gate.resolve(bot, "@nope"))[0] is None
    bot.chats["@person"] = FakeChat(1, type="private", username="person")
    channel, error = run(gate.resolve(bot, "@person"))
    assert channel is None and "not a channel" in error


def test_resolve_rejects_empty_input(gate, bot):
    assert run(gate.resolve(bot, "   "))[0] is None


# ------------------------------------------------- reference normalisation
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@nativecodes", "@nativecodes"),
        ("nativecodes", "@nativecodes"),          # the case that used to break
        ("https://t.me/nativecodes", "@nativecodes"),
        ("http://t.me/nativecodes", "@nativecodes"),
        ("t.me/nativecodes", "@nativecodes"),
        ("telegram.me/nativecodes", "@nativecodes"),
        ("https://t.me/nativecodes/", "@nativecodes"),
        ("  @nativecodes  ", "@nativecodes"),
        ("-1001234567890", "-1001234567890"),
        ("-1001234567890 ", "-1001234567890"),
        ("", None),
        ("   ", None),
        ("https://t.me/+AbCdEf", None),           # invite link, not a reference
        ("t.me/+AbCdEf", None),
        ("has spaces", None),
    ],
)
def test_channel_reference_normalisation(raw, expected):
    assert normalize_channel_ref(raw) == expected


def test_seeded_channels_are_normalised(config, db, tmp_path):
    """REQUIRED_CHANNELS=@a,plain,https://t.me/c must not silently disable the gate."""
    from gmailbot.app import build_application
    from gmailbot.config import Config

    env = {
        "BOT_TOKEN": "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        "GMAIL_EMAIL": "owner@gmail.com",
        "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        "ADMIN_USER_ID": "42",
        "FEEDBACK_CHANNEL_ID": "-1001234567890",
        "DB_PATH": str(tmp_path / "seed.db"),
        "LOG_PATH": str(tmp_path / "seed.log"),
        "REQUIRED_CHANNELS": "@first,second,https://t.me/third,-100999,-100888,bad ref",
    }
    seeded = Config.from_env(env, root=tmp_path)
    application = build_application(seeded)
    stored = [c.chat_id for c in application.bot_data["db"].list_required_channels()]
    assert stored == ["@first", "@second", "@third", "-100999", "-100888"]
    application.bot_data["db"].close()
