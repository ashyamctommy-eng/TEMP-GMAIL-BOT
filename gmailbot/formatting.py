"""Message rendering: one place that builds every screen.

The original inlined the same OTP listing twice (``/otp`` and the ``view_otp``
callback) and the same alias-message listing twice (``/view`` and ``view_<alias>``),
so the two copies had already drifted. It also interpolated raw email subjects
and bodies into Markdown, which is why every send had a ``try/except`` that
re-sent the same text without ``parse_mode`` -- and why "Copy Link" handed users
truncated URLs.

Rules here:

* user-derived text is always HTML-escaped,
* links are rendered with the **full** URL in ``href`` and a short *label*,
* OTPs go in ``<code>`` so clients offer tap-to-copy,
* output is clamped to Telegram's 4096-character limit with balanced tags.
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import KeyboardButtonStyle

#: Button colours. Clients released before 9 Feb 2026 ignore `style` and draw
#: their default colour, so the emoji in each label stays meaningful on its own.
STYLE_PRIMARY = KeyboardButtonStyle.PRIMARY  # blue
STYLE_SUCCESS = KeyboardButtonStyle.SUCCESS  # green
STYLE_DANGER = KeyboardButtonStyle.DANGER    # red

from . import callbacks as cb
from .models import Alias, Message
TELEGRAM_TEXT_LIMIT = 4096
#: Telegram caps photo captions at 1024 characters (UTF-16 units, like text).
CAPTION_LIMIT = 1024
BODY_PREVIEW_CHARS = 220
SUBJECT_PREVIEW_CHARS = 90
MESSAGES_PER_PAGE = 5
OTP_ENTRIES_PER_PAGE = 5

_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "span"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)>")


def utf16_len(value: str) -> int:
    """Length as Telegram counts it.

    Telegram measures in UTF-16 code units, not code points: every character
    outside the BMP (which includes the styled brand font and most emoji) costs
    two. Counting ``len()`` would let a branded message look 19 characters
    shorter than it is and silently exceed the limit.
    """
    return len(value.encode("utf-16-le")) // 2


def _fit(text: str, budget: int) -> str:
    """Longest prefix of ``text`` that fits ``budget`` UTF-16 units.

    Binary search, not a linear estimate: with the styled brand font and emoji
    every character can cost two units, so "remove N characters" over-trims by
    up to half. Slicing never splits a surrogate pair because Python strings are
    sequences of code points.
    """
    if utf16_len(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if utf16_len(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def esc(value: object) -> str:
    """Escape for ``parse_mode=HTML``."""
    return html.escape("" if value is None else str(value), quote=False)


def esc_attr(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def pre(value: object) -> str:
    return f"<pre>{esc(value)}</pre>"


def balance_html(text: str) -> str:
    """Close any tags left open (used after clamping)."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        closing, tag, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if tag not in _ALLOWED_TAGS or attrs.rstrip().endswith("/"):
            continue
        if closing:
            if tag in stack:
                while stack and stack.pop() != tag:
                    pass
        else:
            stack.append(tag)
    return text + "".join(f"</{tag}>" for tag in reversed(stack))


def clamp(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Truncate to ``limit`` characters *after* closing the tags we cut through.

    The closing tags are part of the budget, otherwise a heavily tagged message
    ends up over the limit again and Telegram rejects it outright.
    """
    if utf16_len(text) <= limit:
        return text
    budget = limit
    balanced = text
    for _ in range(6):
        cut = _fit(balanced, max(budget - 1, 1))
        # Never cut inside a tag.
        if cut.rfind("<") > cut.rfind(">"):
            cut = cut[: cut.rfind("<")]
        body = cut.rstrip() + "…"
        balanced = balance_html(body)
        if utf16_len(balanced) <= limit:
            return balanced
        budget = limit - (utf16_len(balanced) - utf16_len(body))
    return _fit(balanced, limit)  # pragma: no cover - pathological input


def credit_line(config) -> str:
    return config.credit_line


def brand_caption(config) -> str:
    """Short caption used when the full message will not fit in a caption."""
    return (
        f"<b>{esc(config.brand_name)}</b>\n\n"
        f"<i>New message below ↓</i>\n\n{esc(config.credit_line)}"
    )


def finalize(text: str, config, *, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Every user-facing message leaves through here: brand footer + clamp.

    The footer is budgeted *before* clamping, so branding can never push a
    message over Telegram's limit, and it is never appended twice.
    """
    body = (text or "").rstrip()
    mark = config.credit_line
    if mark and mark in body:
        return clamp(body, limit=limit)
    foot = f"\n\n{mark}" if mark else ""
    room = max(limit - utf16_len(foot), 1)
    return clamp(body, limit=room) + foot


def clamp_for_caption(text: str, config) -> tuple[str, str | None]:
    """Return ``(caption, overflow_message)`` for a photo send.

    Never truncates: if the branded text does not fit a caption it becomes a
    second message, because cutting an OTP alert in half could lose the code.
    """
    full = finalize(text, config)
    if utf16_len(full) <= CAPTION_LIMIT:
        return full, None
    return finalize(brand_caption(config), config), full


def clip(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def yes_no(value: bool) -> str:
    return "✅ active" if value else "⛔ deleted"


def duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


# --------------------------------------------------------------------- keyboards
def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎲 New alias", callback_data=cb.GEN)],
            [
                InlineKeyboardButton("🔑 OTPs", callback_data=cb.OTP_LIST),
                InlineKeyboardButton("📋 My aliases", callback_data=cb.HISTORY),
            ],
            [InlineKeyboardButton("💬 Feedback", callback_data=cb.FEEDBACK)],
        ]
    )


def pager_row(prefix_alias: str, page: int, has_more: bool) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "⬅️ Newer", callback_data=cb.view_alias(prefix_alias, page - 1)
            )
        )
    if has_more:
        row.append(
            InlineKeyboardButton(
                "Older ➡️", callback_data=cb.view_alias(prefix_alias, page + 1)
            )
        )
    return row


# ---------------------------------------------------------------------- screens
def welcome(config, *, max_aliases: int) -> str:
    return (
        f"🤖 <b>{esc(config.brand_name)}</b>\n\n"
        f"Generate unlimited Gmail aliases on <code>{esc(config.alias_address)}</code> "
        "and receive the mail (and OTP codes) right here.\n\n"
        "<b>Commands</b>\n"
        "/generate – new random alias\n"
        "/generate &lt;name&gt; – pick your own\n"
        "/history – your aliases\n"
        "/view &lt;alias&gt; – messages for one alias\n"
        "/otp – recent codes and verification links\n"
        "/delete &lt;alias&gt; – stop showing an alias\n"
        "/feedback – tell the admin something\n\n"
        f"Messages are kept for {esc(duration(config.message_ttl_seconds))} and then "
        f"deleted automatically. Up to {max_aliases} aliases per account.\n\n"
        "Tap <b>New alias</b> to get started."
    )


def alias_created(config, alias: str, label: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "✅ <b>Alias ready</b>\n\n"
        f"{code(config.full_alias(alias))}\n\n"
        f"Style: <b>{esc(label)}</b>\n"
        "• Give this address to the site you're signing up for\n"
        "• Mail arrives here automatically\n"
        "• OTP codes and verification links are detected for you\n"
        f"• Codes are deleted after {esc(duration(config.message_ttl_seconds))}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👀 Messages", callback_data=cb.view_alias(alias))],
            [
                InlineKeyboardButton("🔑 OTPs", callback_data=cb.OTP_LIST),
                InlineKeyboardButton("🎲 Another", callback_data=cb.GEN),
            ],
            [InlineKeyboardButton("💬 Feedback", callback_data=cb.FEEDBACK)],
        ]
    )
    return text, keyboard


def alias_list(config, aliases: Sequence[Alias]) -> tuple[str, InlineKeyboardMarkup]:
    if not aliases:
        return (
            "📭 You have no aliases yet.\n\nCreate one with /generate.",
            menu_keyboard(),
        )
    lines = ["📋 <b>Your aliases</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for alias in aliases:
        lines.append(
            f"{'✅' if alias.active else '⛔'} {code(config.full_alias(alias.name))}"
        )
        if alias.active:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"👀 {alias.name[:18]}", callback_data=cb.view_alias(alias.name)
                    ),
                    InlineKeyboardButton("🗑", callback_data=cb.delete_alias(alias.name)),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"♻️ Restore {alias.name[:16]}",
                        callback_data=cb.restore_alias(alias.name),
                    )
                ]
            )
    rows.append(
        [
            InlineKeyboardButton("🎲 New alias", callback_data=cb.GEN),
            InlineKeyboardButton("🔑 OTPs", callback_data=cb.OTP_LIST),
        ]
    )
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def _entry_header(message: Message) -> str:
    return (
        f"📨 <b>{esc(clip(message.subject, SUBJECT_PREVIEW_CHARS))}</b> "
        f"<i>{esc(message.received_display)}</i>"
    )


def _entry_body(message: Message) -> list[str]:
    lines: list[str] = []
    if message.otp:
        lines.append(f"🔑 OTP: {code(message.otp)}")
    if message.links:
        first = message.links[0]
        host = esc(_host_of(first))
        lines.append(f'🔗 <a href="{esc_attr(first)}">{host}</a>')
        if len(message.links) > 1:
            lines.append(
                f"<i>+{len(message.links) - 1} more link(s) — open buttons below</i>"
            )
    if message.body:
        lines.append(pre(clip(message.body, BODY_PREVIEW_CHARS)))
    return lines


def _host_of(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def _link_row(message: Message) -> list[InlineKeyboardButton]:
    """Open the link in one tap, or copy it in one tap.

    A URL button hands the link to Telegram itself, so magic links need no
    select-and-edit dance -- which was the whole complaint. The copy button
    sends the same URL inside <code> for clients/people who want it on the
    clipboard.
    """
    links = list(message.links)
    if not links:
        return []
    primary = links[0]
    row = [
        InlineKeyboardButton(
            f"🔓 Open {clip(_host_of(primary), 20)}",
            url=primary,
            style=STYLE_PRIMARY,
        ),
        InlineKeyboardButton(
            "📋 Copy link", callback_data=cb.reveal_secret(message.id, "link"),
            style=STYLE_SUCCESS,
        ),
    ]
    return row


def _secret_buttons(messages: Sequence[Message]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for message in messages:
        row = _link_row(message)
        if row:
            rows.append(row)
        # Additional links in the same mail get their own open buttons (max 3).
        for extra in list(message.links)[1:3]:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🔗 Open {clip(_host_of(extra), 20)}",
                        url=extra,
                        style=STYLE_PRIMARY,
                    )
                ]
            )
    return rows


def otp_digest(
    messages: Sequence[Message], *, page: int = 0, total: int | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Recent codes / links, newest first, paginated."""
    if not messages:
        return (
            "📭 No OTP codes or verification links in the last "
            "batch of messages.\n\nThey are deleted automatically after the TTL.",
            menu_keyboard(),
        )
    start = page * OTP_ENTRIES_PER_PAGE
    window = list(messages[start : start + OTP_ENTRIES_PER_PAGE])
    total = len(messages) if total is None else total
    lines = ["🔑 <b>Recent codes</b>\n"]
    for message in window:
        lines.append(_entry_header(message))
        lines.append(f"👤 {esc(message.alias)}")
        lines.extend(_entry_body(message))
        lines.append("")
    rows = _secret_buttons(window)
    pager = []
    if page > 0:
        pager.append(InlineKeyboardButton("⬅️ Newer", callback_data=cb.otp_page(page - 1)))
    if start + OTP_ENTRIES_PER_PAGE < total:
        pager.append(InlineKeyboardButton("Older ➡️", callback_data=cb.otp_page(page + 1)))
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton("💬 Feedback", callback_data=cb.FEEDBACK)])
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def alias_messages(
    alias: str,
    messages: Sequence[Message],
    *,
    page: int = 0,
    total: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(messages) if total is None else total
    if not messages:
        text = (
            f"📭 No messages for {code(alias)} yet.\n\n"
            "New mail shows up here automatically — tap refresh in a moment."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Refresh", callback_data=cb.view_alias(alias))],
                [
                    InlineKeyboardButton("🔑 OTPs", callback_data=cb.OTP_LIST),
                    InlineKeyboardButton("🎲 New alias", callback_data=cb.GEN),
                ],
            ]
        )
        return text, keyboard

    lines = [f"📧 <b>{esc(alias)}</b> — {total} message(s)\n"]
    for message in messages:
        lines.append(_entry_header(message))
        if message.sender:
            lines.append(f"👤 <i>{esc(clip(message.sender, 60))}</i>")
        lines.extend(_entry_body(message))
        lines.append("")
    rows = _secret_buttons(messages)
    has_more = (page + 1) * MESSAGES_PER_PAGE < total
    pager = pager_row(alias, page, has_more)
    if pager:
        rows.append(pager)
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=cb.view_alias(alias, page)),
            InlineKeyboardButton("🔑 OTPs", callback_data=cb.OTP_LIST),
        ]
    )
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def secret_reveal(label: str, value: str, action_hint: str) -> str:
    return f"{label}\n{code(value)}\n\n<i>{esc(action_hint)}</i>"


def link_copy(value: str) -> str:
    """Shown after pressing Copy link: the whole URL, tappable, nothing else."""
    return (
        "📋 <b>Verification link</b>\n"
        f"{code(value)}\n\n"
        "<i>Tap the link above to copy it.</i>"
    )


def feedback_prompt() -> str:
    return (
        "💬 <b>Feedback</b>\n\n"
        "Send any message — or a photo with a caption — and the admin will see it.\n\n"
        "Your Telegram name and user id are attached so replies are possible.\n"
        "Send /cancel to leave feedback mode."
    )


# ------------------------------------------------------------------ force join
def join_required(channels: Sequence) -> tuple[str, InlineKeyboardMarkup]:
    """The gate screen: one link button per channel, plus a verify button."""
    lines = ["🔒 <b>Join required</b>", "", "Join to use this bot:"]
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        lines.append(f"• <b>{esc(channel.display)}</b>")
        if channel.invite_link:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"➡️ Join {clip(channel.display, 26)}",
                        url=channel.invite_link,
                        style=STYLE_PRIMARY,
                    )
                ]
            )
    lines += ["", "Then tap <b>I've joined</b> below."]
    rows.append(
        [InlineKeyboardButton("✅ I've joined", callback_data=cb.JOIN_VERIFY, style=STYLE_SUCCESS)]
    )
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


# ----------------------------------------------------------------- admin panel
def admin_panel(channels: Sequence, *, stats: dict[str, int] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🛠 <b>Admin panel</b>", "", "Use the buttons below."]
    if stats:
        lines = [
            "🛠 <b>Admin panel</b>",
            "",
            f"👥 Users: {stats.get('users', 0)} ({stats.get('banned', 0)} banned)",
            f"📧 Aliases: {stats.get('aliases', 0)} ({stats.get('active_aliases', 0)} active)",
            f"✉️ Messages: {stats.get('messages', 0)}",
            f"📣 Required channels: {len(channels)}",
        ]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📊 Stats", callback_data=cb.ADMIN_STATS, style=STYLE_PRIMARY),
            InlineKeyboardButton("📣 Broadcast", callback_data=cb.ADMIN_BROADCAST, style=STYLE_PRIMARY),
        ],
        [
            InlineKeyboardButton("🚫 Ban", callback_data=cb.ADMIN_BAN, style=STYLE_DANGER),
            InlineKeyboardButton("✅ Unban", callback_data=cb.ADMIN_UNBAN, style=STYLE_SUCCESS),
        ],
        [
            InlineKeyboardButton("➕ Add channel", callback_data=cb.ADMIN_ADD_CHANNEL, style=STYLE_SUCCESS),
            InlineKeyboardButton("➖ Remove channel", callback_data=cb.ADMIN_DEL_CHANNEL, style=STYLE_DANGER),
        ],
        [
            InlineKeyboardButton("📋 Manage channels", callback_data=cb.ADMIN_CHANNELS, style=STYLE_PRIMARY),
            InlineKeyboardButton("🔄 Refresh", callback_data=cb.ADMIN_PANEL, style=STYLE_PRIMARY),
        ],
        [InlineKeyboardButton("✖️ Close", callback_data=cb.ADMIN_CLOSE, style=STYLE_DANGER)],
    ]
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def channels_admin(channels: Sequence) -> tuple[str, InlineKeyboardMarkup]:
    if not channels:
        return (
            "📣 <b>Required channels</b>\n\nNone yet — the bot is open to everyone.\n"
            "Add one with ➕ Add channel.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Add channel", callback_data=cb.ADMIN_ADD_CHANNEL, style=STYLE_SUCCESS)],
                    [InlineKeyboardButton("⬅️ Back", callback_data=cb.ADMIN_PANEL, style=STYLE_PRIMARY)],
                ]
            ),
        )
    lines = ["📣 <b>Required channels</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for index, channel in enumerate(channels, start=1):
        link = f" — {esc(channel.invite_link)}" if channel.invite_link else ""
        lines.append(f"{index}. <b>{esc(channel.display)}</b> <code>{esc(channel.chat_id)}</code>{link}")
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗑 Remove {clip(channel.display, 20)}",
                    callback_data=cb.remove_channel(channel.chat_id),
                    style=STYLE_DANGER,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("➕ Add channel", callback_data=cb.ADMIN_ADD_CHANNEL, style=STYLE_SUCCESS),
            InlineKeyboardButton("⬅️ Back", callback_data=cb.ADMIN_PANEL, style=STYLE_PRIMARY),
        ]
    )
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def error(message: str) -> str:
    return f"❌ {esc(message)}"


def notice(message: str) -> str:
    return f"ℹ️ {esc(message)}"


def otp_notification(config, message: Message) -> tuple[str, InlineKeyboardMarkup]:
    """Heads-up sent when new mail with a code (or just a magic link) arrives."""
    headline = "New code" if message.otp else "New link"
    lines = [f"🔔 <b>{headline}</b>\n", _entry_header(message)]
    lines.append(f"👤 {esc(message.alias)}")
    lines.extend(_entry_body(message))
    rows: list[list[InlineKeyboardButton]] = []
    if message.links:
        rows.append(_link_row(message))
    rows.append(
        [
            InlineKeyboardButton(
                "👀 View messages", callback_data=cb.view_alias(message.alias)
            ),
            InlineKeyboardButton("🔑 All OTPs", callback_data=cb.OTP_LIST),
        ]
    )
    lines.append(f"\n⏰ Kept for {duration(config.message_ttl_seconds)}.")
    return clamp("\n".join(lines)), InlineKeyboardMarkup(rows)


def mail_notification(config, message: Message) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📧 <b>New email</b>\n\n"
        f"👤 {esc(message.alias)}\n"
        f"📨 {esc(clip(message.subject, SUBJECT_PREVIEW_CHARS))}\n"
        f"🕒 {esc(message.received_display)}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👀 Read it", callback_data=cb.view_alias(message.alias)
                )
            ]
        ]
    )
    return text, keyboard


def feedback_forward(
    *, header_lines: Iterable[str], feedback_id: int, body: str, escaped_body: bool = False
) -> str:
    """Body text forwarded to the admin channel.

    ``escaped_body`` must be True when the caller already escaped it, so we never
    double-escape; user text is *always* escaped at least once, which is what
    stops a user from forging the identity header above.
    """
    header = "\n".join(header_lines)
    payload = body if escaped_body else esc(body)
    return (
        f"{header}\n"
        f"<b>Feedback ID:</b> {feedback_id}\n\n"
        f"<blockquote>{payload}</blockquote>"
    )
