#!/usr/bin/env python3
"""Drive the real bot code and record what it emits, for the visual preview.

This is not a mockup: it instantiates the actual ``BotHandlers``, the actual
``GmailPoller``, the real OTP/link extractors, the real renderer and the real
SQLite layer -- only the two transports at the edges (Telegram, IMAP) are
swapped for the fakes from ``tests/``. Every string in ``preview/session.json``
was produced by ``gmailbot`` itself.

    python evidence/preview_session.py
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmailbot.aliases import AliasGenerator  # noqa: E402
from gmailbot.config import Config  # noqa: E402
from gmailbot.db import Database  # noqa: E402
from gmailbot.handlers import (  # noqa: E402
    BotHandlers,
    ChannelGuard,
    build_notification,
)
from gmailbot.mail import GmailPoller  # noqa: E402
from gmailbot.notify import Notifier  # noqa: E402
from gmailbot.otp import OtpExtractor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeBot,
    FakeChatMember,
    FakeContext,
    FakeMailbox,
    FakeMessage,
    FakeQuery,
    FakeUpdate,
    FakeUser,
    FakeImap,
    make_raw_email,
)

USER_ID = 7
ADMIN_ID = 42
CHANNEL_LABEL = "channel"
events: list[dict] = []

CHAT_LABELS = {
    "user": "@theta_test · TempGail",
    "channel": "Feedback channel · admin only",
    "admin": "@owner · TempGail (admin)",
}


def record(chat: str, role: str, text: str, buttons=None, note: str | None = None) -> None:
    entry: dict = {"chat": chat, "role": role, "text": text}
    if buttons:
        entry["buttons"] = buttons
    if note:
        entry["note"] = note
    events.append(entry)


def button_rows(markup) -> list[list[str]] | None:
    if markup is None:
        return None
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return None
    return [[button.text for button in row] for row in rows]


def drain_channel(bot: FakeBot, *, label: str = CHANNEL_LABEL) -> None:
    """Record anything the handlers pushed to the admin channel."""
    while bot.messages:
        chat_id, text, _ = bot.messages.pop(0)
        record(label, "channel", text, note="delivered to the feedback channel")


def drain(message: FakeMessage, chat: str = "user") -> None:
    while message.sent:
        sent = message.sent.pop(0)
        record(chat, "out", sent.text, button_rows(sent.markup))


def drain_query(query: FakeQuery) -> None:
    while query.edits:
        sent = query.edits.pop(0)
        record("user", "out", sent.text, button_rows(sent.markup))


def in_bubble(text: str, chat: str = "user") -> None:
    record(chat, "in", text)


def build(tmp: Path):
    env = {
        "BOT_TOKEN": "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        "GMAIL_EMAIL": "tempgail.inbox@gmail.com",
        "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        "ADMIN_USER_ID": str(ADMIN_ID),
        "FEEDBACK_CHANNEL_ID": "-1001234567890",
        "DB_PATH": str(tmp / "preview.db"),
        "LOG_PATH": str(tmp / "preview.log"),
        "LOG_LEVEL": "WARNING",
    }
    config = Config.from_env(env, root=tmp)
    db = Database(config.db_path)
    bot = FakeBot(FakeChatMember("administrator"))
    notifier = Notifier(build_notification(config))
    handlers = BotHandlers(
        config,
        db,
        generator=AliasGenerator(rng=random.Random(2026), formats=["adjective+noun"]),
        extractor=OtpExtractor(),
        notifier=notifier,
        guard=ChannelGuard(config.feedback_channel_id),
    )
    return config, db, bot, handlers


def run_command(handlers, bot, name: str, args=None, *, user_id=USER_ID, typed=None):
    message = FakeMessage(text=typed or f"/{name}")
    update = FakeUpdate(user=FakeUser(user_id, first_name="Theta", username="theta_test"), message=message)
    context = FakeContext(bot=bot, args=args or [])
    in_bubble(typed or (f"/{name} " + " ".join(args) if args else f"/{name}"))
    asyncio.run(getattr(handlers, name)(update, context))
    drain(message, "admin" if user_id == ADMIN_ID else "user")
    return message, context


def press_button(handlers, bot, data: str, *, user_id=USER_ID):
    message = FakeMessage(text="(message with buttons)")
    query = FakeQuery(data=data, user=FakeUser(user_id, first_name="Theta", username="theta_test"), message=message)
    update = FakeUpdate(user=FakeUser(user_id), message=message, query=query)
    context = FakeContext(bot=bot)
    in_bubble(f"‹button press› {data}")
    asyncio.run(handlers.on_callback(update, context))
    drain_query(query)
    drain(message)


def main() -> int:
    tmp = Path("/tmp/preview_run")
    tmp.mkdir(parents=True, exist_ok=True)
    for leftover in tmp.glob("preview.db*"):
        leftover.unlink()
    config, db, bot, handlers = build(tmp)

    # 1. onboarding ---------------------------------------------------------
    run_command(handlers, bot, "start")

    # 2. a generated alias --------------------------------------------------
    message, context = run_command(handlers, bot, "generate")
    alias = db.list_aliases(USER_ID)[0]
    address = config.full_alias(alias.name)

    # 3. mail arrives -------------------------------------------------------
    mailbox = FakeMailbox()
    mailbox.add(
        1,
        make_raw_email(
            to=address,
            subject="Your Acme ID verification code",
            body=(
                "Your Acme ID verification code is 483920.\n\n"
                "Enter it to finish signing in. It expires in 10 minutes.\n"
                "If you didn't try to sign in, you can ignore this email.\n\n"
                "Verify now: https://id.acme.example/verify?token=8f3a91&continue=1"
            ),
            message_id="<acme-1@acme.example>",
            sender="Acme ID <no-reply@acme.example>",
        ),
    )
    mailbox.add(
        2,
        make_raw_email(
            to=address,
            subject="Confirm your email address",
            body="Welcome to Vaultly! Use code 918273 to confirm your address.",
            html=(
                '<html><body style="font-family:sans-serif">'
                "<h2>Welcome to Vaultly</h2>"
                "<p>Use code <b>918273</b> to confirm your email address.</p>"
                '<p><a href="https://app.vaultly.example/confirm?code=918273">Confirm email</a></p>'
                '<img src="https://cdn.vaultly.example/pixel.gif" width="1" height="1">'
                '<p style="color:#888"><a href="https://vaultly.example/unsubscribe?u=991">Unsubscribe</a></p>'
                "</body></html>"
            ),
            message_id="<vaultly-1@vaultly.example>",
            sender="Vaultly <hello@vaultly.example>",
        ),
    )
    mailbox.add(
        3,
        make_raw_email(
            to=address,
            subject="Your Northwind order 5691 has shipped",
            body=(
                "Order 5691 is on its way to 90210.\n"
                "Total: $49.99. Tracking: https://northwind.example/track/5691"
            ),
            message_id="<northwind-1@northwind.example>",
            sender="Northwind <orders@northwind.example>",
        ),
    )
    mailbox.add(
        4,
        make_raw_email(
            to=address,
            subject="Your Globex sign-in code",
            body="Your Globex code is 123-456. Never share it with anyone.",
            message_id="<globex-1@globex.example>",
            sender="Globex <security@globex.example>",
        ),
    )

    render = build_notification(config)

    def on_message(stored) -> None:
        notification = render(stored)
        record(
            "user",
            "out",
            notification.text,
            button_rows(notification.markup),
            note="push alert (mail poller)",
        )

    poller = GmailPoller(
        config,
        db,
        on_message=on_message,
        client_factory=lambda host: FakeImap(mailbox),
        extractor=handlers.extractor,
        sleep=lambda seconds: None,
    )
    record("user", "note", "✉️ 4 new emails arrive in the mailbox…")
    poller.run_once()

    # 4. codes, messages, history ------------------------------------------
    run_command(handlers, bot, "otp")
    run_command(handlers, bot, "view", [alias.name])
    run_command(handlers, bot, "history")
    press_button(handlers, bot, f"v:{alias.name}:0")
    press_button(handlers, bot, "a:otp:0")

    # 5. free text is not an alias -----------------------------------------
    run_command(handlers, bot, "on_text", typed="hi")

    # 6. feedback ----------------------------------------------------------
    run_command(handlers, bot, "feedback")
    message = FakeMessage(text="the webhook retry bit saved me an hour, thanks!")
    update = FakeUpdate(
        user=FakeUser(USER_ID, first_name="Theta", last_name="Tester", username="theta_test"),
        message=message,
    )
    context = FakeContext(bot=bot, user_data={"mode": "feedback", "uid": USER_ID})
    in_bubble("the webhook retry bit saved me an hour, thanks!")
    asyncio.run(handlers.on_text(update, context))
    drain(message)
    drain_channel(bot)

    # 7. admin --------------------------------------------------------------
    record("admin", "note", "↔️ the admin's own chat with the bot")
    run_command(handlers, bot, "stats", user_id=ADMIN_ID)
    run_command(handlers, bot, "broadcast", ["Heads up: codes now auto-detect from HTML-only mail too."], user_id=ADMIN_ID)

    out = ROOT / "preview" / "session.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_by": "evidence/preview_session.py",
                "alias": alias.name,
                "address": address,
                "chat_labels": CHAT_LABELS,
                "stats": db.stats(),
                "events": events,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"alias used   : {address}")
    print(f"events       : {len(events)}")
    print(f"messages row : {db.stats()['messages']} stored, {db.stats()['feedback']} feedback")
    print(f"wrote        : {out.relative_to(ROOT)}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
