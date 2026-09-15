"""Force-join gate: users must be in the configured channels before the bot responds.

Design decisions worth knowing:

* **Fail open, loudly.** If a channel cannot be checked (the bot is not an admin
  there, the channel was deleted, the API errored) the user is allowed through and
  the problem is logged. Failing closed would lock *every* user out of the bot
  because of one misconfiguration, which is a far worse outcome.
* **Cached.** Membership is cached per user for ``membership_cache_seconds``
  (default 5 min). Without this, every command and every button press would cost
  one ``getChatMember`` call per channel and hit Bot API rate limits.
* **Runs as a group -1 TypeHandler** and raises ``ApplicationHandlerStop``, so no
  handler has to remember to check anything.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from telegram import Chat, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from . import callbacks as cb
from . import formatting as fmt
from .db import Database
from .models import RequiredChannel

logger = logging.getLogger(__name__)

#: A user counts as joined in any of these states.
_MEMBER_STATES = {"creator", "administrator", "member", "restricted"}


def normalize_channel_ref(raw: str) -> str | None:
    """Accept every shape a human will type; return what the Bot API needs.

    ``nativecodes``, ``@nativecodes``, ``https://t.me/nativecodes`` and
    ``-1001234567890`` are all valid input. Telegram itself only understands
    ``@username`` or a numeric id, so a bare handle would be unresolvable -- and
    because the gate fails open, that would silently switch the check off.

    Returns ``None`` for anything unusable (empty, an invite link like
    ``t.me/+abc``, or junk).
    """
    value = (raw or "").strip()
    if not value:
        return None
    lowered = value.lower()
    for prefix in (
        "https://t.me/", "http://t.me/", "t.me/",
        "https://telegram.me/", "http://telegram.me/", "telegram.me/",
    ):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip("/").split("/")[0]
    if not value or value.startswith("+"):
        return None  # an invite link is not a usable chat reference
    if value.startswith("@"):
        value = value[1:]
    if value.lstrip("-").isdigit():
        return value
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", value):
        return f"@{value}"
    return None


@dataclass
class _CacheEntry:
    expires_at: float
    missing: tuple[RequiredChannel, ...]


class MembershipGate:
    def __init__(
        self,
        db: Database,
        *,
        enabled: bool = True,
        cache_seconds: int = 300,
        admin_user_id: int | None = None,
        clock=time.monotonic,
    ) -> None:
        self.db = db
        self.enabled = enabled
        self.cache_seconds = max(int(cache_seconds), 5)
        self.admin_user_id = admin_user_id
        self._clock = clock
        self._cache: dict[int, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ check
    async def missing(self, bot, user_id: int) -> list[RequiredChannel]:
        """Channels ``user_id`` still has to join. Cached per user."""
        if not self.enabled:
            return []
        channels = await asyncio.to_thread(self.db.list_required_channels)
        if not channels:
            return []

        entry = self._cache.get(user_id)
        now = self._clock()
        if entry is not None and entry.expires_at > now:
            return list(entry.missing)

        missing: list[RequiredChannel] = []
        for channel in channels:
            try:
                member = await bot.get_chat_member(channel.chat_id, user_id)
                status = str(getattr(member, "status", "") or "")
            except TelegramError as exc:
                # Fail open: a broken check must not block real users.
                logger.warning(
                    "cannot check membership of %s for %s (%s); letting them through",
                    channel.chat_id,
                    user_id,
                    exc,
                )
                continue
            if status not in _MEMBER_STATES:
                missing.append(channel)

        self._cache[user_id] = _CacheEntry(
            expires_at=now + self.cache_seconds, missing=tuple(missing)
        )
        return missing

    def invalidate(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(user_id, None)

    # ------------------------------------------------------------- middleware
    async def middleware(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Registered in group -1: stops non-members before any handler runs."""
        if not self.enabled:
            return
        user = update.effective_user
        if user is None:
            return
        if self.admin_user_id is not None and user.id == self.admin_user_id:
            return  # the owner is never locked out, even during a misconfiguration
        query = update.callback_query
        if query is not None and (query.data or "") == cb.JOIN_VERIFY:
            return  # the verify button must always reach its handler
        if query is not None and (query.data or "").startswith(cb.ADMIN_PREFIX):
            return  # on_callback re-checks admin rights for these anyway

        missing = await self.missing(context.bot, user.id)
        if not missing:
            return

        await self._prompt(update, missing)
        raise ApplicationHandlerStop

    async def _prompt(self, update: Update, missing: list[RequiredChannel]) -> None:
        text, markup = fmt.join_required(missing)
        query = update.callback_query
        try:
            if query is not None:
                await query.answer("Join the channel first.", show_alert=False)
                target = query.message
                if target is not None:
                    await target.reply_text(text, reply_markup=markup, parse_mode="HTML")
                return
            message = update.effective_message
            if message is not None:
                await message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramError as exc:  # pragma: no cover - transport level
            logger.warning("could not send the join prompt: %s", exc)

    # ------------------------------------------------------------- moderation
    async def resolve(self, bot, raw: str) -> tuple[RequiredChannel | None, str | None]:
        """Turn user input (`@name`, a t.me link or an id) into a channel record.

        Returns ``(channel, error)``. The invite link is derived when possible so
        admins only have to pass the handle.
        """
        candidate = (raw or "").strip()
        error: str | None = None
        if not candidate:
            return None, "Give me a channel @username or id."
        if candidate.startswith(("https://t.me/", "http://t.me/", "t.me/")):
            candidate = "@" + candidate.split("t.me/", 1)[1].strip("/").split("/", 1)[0]
        if not candidate.startswith("@") and not candidate.lstrip("-").isdigit():
            if candidate.lstrip("-").isdigit() or " " not in candidate:
                candidate = f"@{candidate}"
            else:
                return None, "That does not look like a channel."

        try:
            chat = await bot.get_chat(candidate)
        except TelegramError as exc:
            return None, f"Telegram could not resolve {candidate}: {exc}"

        if chat.type not in (Chat.CHANNEL, Chat.SUPERGROUP, Chat.GROUP):
            return None, f"{candidate} is a {chat.type}, not a channel or group."

        chat_id = f"@{chat.username}" if chat.username else str(chat.id)
        link = chat.invite_link
        if not link and chat.username:
            link = f"https://t.me/{chat.username}"
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat_id)
            except TelegramError as exc:
                error = (
                    "no invite link available yet (make the bot an admin in the "
                    f"channel to get one): {exc}"
                )

        # A membership check only works reliably when the bot administrates.
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
            if str(getattr(me, "status", "")) not in ("administrator", "creator"):
                error = "the bot is not an admin in that channel, so join checks may fail"
        except TelegramError as exc:
            error = f"cannot read member status there: {exc}"

        return RequiredChannel(chat_id=chat_id, title=chat.title or chat_id, invite_link=link), error
