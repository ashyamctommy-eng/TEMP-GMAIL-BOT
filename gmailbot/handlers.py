"""Telegram handlers.

Behaviour changes vs. the original (docs/IMPROVEMENTS.md):

* P0-8  Free text no longer silently creates an alias. The original matched any
  word against ``^[a-zA-Z0-9_-]{1,20}$`` and created an alias named "hi" when a
  user said hello.
* P0-9  Everything sent with ``parse_mode`` is escaped once, in
  :mod:`gmailbot.formatting`. The original shoved raw subjects/bodies/feedback
  into Markdown and then re-sent the message without formatting whenever
  Telegram rejected it -- which also let a user forge the admin-facing
  "User Information" header with their own markdown.
* P0-10 "Copy OTP" buttons no longer echo secrets back into the chat; codes and
  links are rendered as ``<code>`` (tap to copy) and full links are fetched from
  the database with an ownership check, so they are never truncated.
* P1-5  The feedback-channel permission check is cached. The original ran
  ``get_chat`` + ``get_chat_member`` + a test send + a delete on *every*
  ``/feedback`` press, including an unauthenticated test message in the admin
  channel.
* P1-6  ``/broadcast`` respects flood control instead of firing in a tight loop.
* P2-4  One rendering module and one callback schema replace the duplicated
  inline strings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message as TgMessage,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import callbacks as cb
from . import formatting as fmt
from .aliases import AliasGenerator, label_for, normalize, validation_error
from .config import Config
from .db import Database
from .models import Message, StoredMessage
from .notify import Notification, Notifier, RateLimiter
from .otp import OtpExtractor

logger = logging.getLogger(__name__)

PAGE_SIZE = fmt.MESSAGES_PER_PAGE
ADMIN_COMMANDS = ("ban", "unban", "broadcast", "stats")


class ChannelGuard:
    """Cached permission probe for the feedback channel."""

    def __init__(self, channel_id: int, *, ttl: float = 600.0) -> None:
        self.channel_id = channel_id
        self.ttl = ttl
        self._checked_at = 0.0
        self._ok = False
        self._reason = "not checked yet"

    async def ensure(self, bot) -> tuple[bool, str]:
        now = time.monotonic()
        if now - self._checked_at < self.ttl:
            return self._ok, self._reason
        try:
            member = await bot.get_chat_member(self.channel_id, bot.id)
        except TelegramError as exc:
            self._record(False, f"cannot read the channel: {exc}")
            return self._ok, self._reason

        # Duck-typed on purpose: python-telegram-bot reshuffles the ChatMember*
        # class hierarchy between majors, and an isinstance chain that silently
        # stops matching looks exactly like "the bot lost its permissions".
        status = str(getattr(member, "status", "") or "")
        if status in ("administrator", "creator"):
            ok = True
        elif status in ("member", "restricted"):
            ok = bool(getattr(member, "can_send_messages", False))
        else:
            ok = False
        self._record(ok, "ok" if ok else f"bot status in channel: {status or 'unknown'}")
        return self._ok, self._reason

    def _record(self, ok: bool, reason: str) -> None:
        self._checked_at = time.monotonic()
        if ok != self._ok or reason != self._reason:
            (logger.info if ok else logger.error)("feedback channel: %s", reason)
        self._ok, self._reason = ok, reason

    def invalidate(self) -> None:
        self._checked_at = 0.0


class BotHandlers:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        generator: AliasGenerator,
        extractor: OtpExtractor,
        notifier: Notifier,
        guard: ChannelGuard | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.generator = generator
        self.extractor = extractor
        self.notifier = notifier
        self.guard = guard or ChannelGuard(config.feedback_channel_id)
        self.generate_limit = RateLimiter(
            config.generate_per_hour, 3600, clock=getattr(time, "monotonic")
        )

    # ------------------------------------------------------------- register
    def register(self, application: Application) -> None:
        commands = {
            "start": self.start,
            "help": self.help,
            "generate": self.generate,
            "history": self.history,
            "view": self.view,
            "delete": self.delete,
            "otp": self.otp,
            "feedback": self.feedback,
            "cancel": self.cancel,
            "ban": self.ban,
            "unban": self.unban,
            "broadcast": self.broadcast,
            "stats": self.stats,
        }
        for name, handler in commands.items():
            application.add_handler(CommandHandler(name, handler))
        application.add_handler(CallbackQueryHandler(self.on_callback))
        application.add_handler(
            MessageHandler(filters.PHOTO & ~filters.COMMAND, self.on_photo)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )
        application.add_error_handler(self.on_error)

    # --------------------------------------------------------------- helpers
    async def _db(self, method: Callable[..., object], *args: object, **kwargs: object):
        """Run a (sync, blocking) database call off the event loop.

        The original called SQLite directly inside ``async def`` handlers; a
        contended write (the IMAP poller holds the same file) stalled the whole
        bot, including its polling loop.
        """
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _reject_if_banned(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return True
        if await self._db(self.db.is_banned, user.id):
            await self._reply(update, fmt.error("You are banned from using this bot."))
            return True
        await self._db(self.db.ensure_user, user.id)
        return False

    def _brand_photo(self) -> str | None:
        """Path to the brand image, or None when it is switched off/missing."""
        if not self.config.send_brand_photo or self.config.brand_photo is None:
            return None
        path = Path(self.config.brand_photo)
        if not path.is_file():
            logger.warning("brand photo missing at %s; sending text only", path)
            return None
        return str(path)

    async def _reply(
        self,
        update: Update,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        *,
        edit: bool = False,
        photo: bool = False,
    ) -> TgMessage | None:
        """Single send path: brand footer, HTML only, optional photo.

        ``BadRequest`` is logged rather than retried blind -- but a rejected
        photo degrades to plain text instead of losing the message.
        """
        query = update.callback_query
        # The credit footer is applied here, once, for every user-facing message.
        full = fmt.finalize(text, self.config)
        try:
            if edit and query is not None:
                return await query.edit_message_text(
                    full, reply_markup=markup, parse_mode=ParseMode.HTML
                )
            target = update.effective_message
            if target is None and query is not None:
                # Button presses carry the message in query.message; PTB usually
                # mirrors it into effective_message but not for every update type.
                target = query.message
            if target is None:  # pragma: no cover - defensive
                return None
            asset = self._brand_photo() if photo else None
            if asset:
                caption, overflow = fmt.clamp_for_caption(text, self.config)
                try:
                    sent = await target.reply_photo(
                        photo=asset,
                        caption=caption,
                        reply_markup=None if overflow else markup,
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError as exc:
                    logger.warning("brand photo rejected (%s); sending text", exc)
                else:
                    if overflow is not None:
                        await target.reply_text(
                            overflow, reply_markup=markup, parse_mode=ParseMode.HTML
                        )
                    return sent
            return await target.reply_text(
                full, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest as exc:
            logger.error("telegram rejected a message: %s | text=%r", exc, text[:200])
            if edit and query is not None:
                try:
                    return await query.message.reply_text(
                        text, reply_markup=markup, parse_mode=ParseMode.HTML
                    )
                except TelegramError as exc2:  # pragma: no cover
                    logger.error("fallback send failed too: %s", exc2)
            return None

    def _admin_only(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == self.config.admin_user_id

    # ---------------------------------------------------------------- basics
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        if context.user_data is not None:
            context.user_data.pop("mode", None)
        await self._reply(
            update,
            fmt.welcome(self.config, max_aliases=self.config.max_aliases_per_user),
            fmt.menu_keyboard(),
            photo=True,
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.start(update, context)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = (context.user_data or {}).pop("mode", None)
        if mode == "feedback":
            await self._reply(update, fmt.notice("Feedback cancelled."))
        else:
            await self._reply(update, fmt.notice("Nothing to cancel."))

    # ----------------------------------------------------------------- alias
    async def _create_alias(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        requested: str | None = None,
        edit: bool = False,
    ) -> None:
        user = update.effective_user
        assert user is not None

        allowed, retry_after = self.generate_limit.check(f"gen:{user.id}")
        if not allowed:
            await self._reply(
                update,
                fmt.error(
                    f"Slow down — you can create {self.config.generate_per_hour} "
                    f"aliases per hour. Try again in {int(retry_after)}s."
                ),
                edit=edit,
            )
            return

        existing = await self._db(self.db.count_aliases, user.id)
        if requested is None and existing >= self.config.max_aliases_per_user:
            await self._reply(
                update,
                fmt.error(
                    f"You already have {existing} aliases (limit "
                    f"{self.config.max_aliases_per_user}). Delete one with /history "
                    "before creating another."
                ),
                markup=fmt.menu_keyboard(),
                edit=edit,
            )
            return

        if requested is not None:
            problem = validation_error(requested)
            if problem:
                await self._reply(update, fmt.error(problem), edit=edit)
                return
            name, label = normalize(requested), None
        else:
            # Uniqueness is enforced by the UNIQUE(alias_name) constraint plus
            # the generator's retry, so no client-side existence scan is needed.
            generated = await asyncio.to_thread(self.generator.generate)
            name, label = generated.name, generated.format

        result = await self._db(self.db.add_alias, user.id, name, label)
        if not result.ok:
            if result.reason == "taken":
                await self._reply(
                    update,
                    fmt.error(
                        f"{name} is already taken by another user — aliases are shared "
                        "across one inbox. Try another name."
                    ),
                    edit=edit,
                )
            else:  # pragma: no cover - defensive
                await self._reply(update, fmt.error("Could not create that alias."), edit=edit)
            return

        if label is None:
            label = label_for(name, None)
        else:
            label = label_for(name, label)
        text, markup = fmt.alias_created(self.config, name, label)
        await self._reply(update, text, markup, edit=edit)

    async def generate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        requested = context.args[0] if context.args else None
        await self._create_alias(update, context, requested=requested)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        user = update.effective_user
        assert user is not None
        aliases = await self._db(self.db.list_aliases, user.id, limit=20)
        text, markup = fmt.alias_list(self.config, aliases)
        await self._reply(update, text, markup)

    async def view(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        if not context.args:
            await self._reply(update, fmt.error("Usage: /view <alias> [page]"))
            return
        page = 0
        if len(context.args) > 1 and context.args[1].isdigit():
            page = max(0, int(context.args[1]) - 1)
        await self._show_alias(update, normalize(context.args[0]), page=page)

    async def _show_alias(self, update: Update, alias: str, *, page: int = 0, edit: bool = False) -> None:
        user = update.effective_user
        assert user is not None
        owned = await self._db(self.db.owned_alias, user.id, alias)
        if owned is None:
            await self._reply(
                update,
                fmt.error(
                    f"No active alias {alias} for your account. "
                    "Use /history to see your aliases."
                ),
                edit=edit,
            )
            return
        total = await self._db(self.db.count_alias_messages, user.id, alias)
        offset = page * PAGE_SIZE
        if offset >= total and total:
            page = 0
            offset = 0
        messages = await self._db(
            self.db.alias_messages, user.id, alias, limit=PAGE_SIZE, offset=offset
        )
        text, markup = fmt.alias_messages(alias, messages, page=page, total=total)
        await self._reply(update, text, markup, edit=edit)
        for message in messages:
            await self._db(self.db.mark_seen, user.id, message.id)

    async def delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        if not context.args:
            await self._reply(update, fmt.error("Usage: /delete <alias>"))
            return
        alias = normalize(context.args[0])
        user = update.effective_user
        assert user is not None
        if await self._db(self.db.set_alias_active, user.id, alias, False):
            await self._reply(
                update,
                fmt.notice(
                    f"{alias} is now inactive — new mail for it is dropped and it no "
                    "longer shows in your list. Use /history to restore it."
                ),
            )
        else:
            await self._reply(update, fmt.error(f"You have no alias called {alias}."))

    async def otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        await self._show_otps(update, page=0, photo=True)

    async def _show_otps(
        self, update: Update, *, page: int = 0, edit: bool = False, photo: bool = False
    ) -> None:
        user = update.effective_user
        assert user is not None
        recent = await self._db(self.db.recent_messages, user.id, limit=60)
        with_secrets = [message for message in recent if message.has_secret]
        text, markup = fmt.otp_digest(with_secrets, page=page, total=len(with_secrets))
        await self._reply(update, text, markup, edit=edit, photo=photo and not edit)

    # -------------------------------------------------------------- feedback
    async def feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_banned(update):
            return
        ok, reason = await self.guard.ensure(context.bot)
        if not ok:
            logger.error("feedback channel unavailable: %s", reason)
            await self._reply(
                update,
                fmt.error("Feedback is temporarily unavailable. Please try again later."),
            )
            return
        if context.user_data is not None:
            context.user_data["mode"] = "feedback"
        await self._reply(update, fmt.feedback_prompt())

    def _feedback_header(self, user) -> list[str]:
        name = " ".join(part for part in (user.first_name, user.last_name) if part)
        username = f"@{user.username}" if user.username else "no username"
        return [
            "📬 <b>Feedback</b>",
            f"👤 {fmt.esc(name or 'unknown')} ({fmt.esc(username)})",
            f"🆔 <code>{user.id}</code>",
        ]

    async def _send_to_channel(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        user_id: int,
        header: list[str],
        body: str,
        photo=None,
    ) -> bool:
        feedback_id = await self._db(
            self.db.save_feedback,
            user_id,
            None if photo is not None else body,
            None if photo is None else photo.file_id,
        )
        payload = fmt.feedback_forward(
            header_lines=header, feedback_id=feedback_id, body=body
        )
        try:
            if photo is None:
                await context.bot.send_message(
                    chat_id=self.config.feedback_channel_id,
                    text=payload,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await context.bot.send_photo(
                    chat_id=self.config.feedback_channel_id,
                    photo=photo.file_id,
                    caption=payload[:1024],
                    parse_mode=ParseMode.HTML,
                )
            return True
        except (BadRequest, TelegramError) as exc:
            logger.error("could not deliver feedback to the channel: %s", exc)
            self.guard.invalidate()
            return False

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None or not message.text:
            return
        if await self._reject_if_banned(update):
            return
        if context.user_data is not None:
            context.user_data["uid"] = user.id

        if (context.user_data or {}).get("mode") == "feedback":
            if context.user_data is not None:
                context.user_data.pop("mode", None)
            sent = await self._send_to_channel(
                context, user_id=user.id, header=self._feedback_header(user),
                body=message.text,
            )
            await self._reply(
                update,
                "✅ Thanks — your feedback reached the admin."
                if sent
                else fmt.error("Could not deliver your feedback. Please try again later."),
            )
            return

        candidate = message.text.strip()
        problem = validation_error(candidate)
        if problem is None:
            # Ask before creating: the original created an alias for *any* word
            # the user typed, so "hi" silently became an alias.
            text = (
                f"Did you mean to create an alias named {fmt.code(normalize(candidate))}?\n\n"
                "It becomes "
                f"{fmt.code(self.config.full_alias(normalize(candidate)))}"
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Create it", callback_data=cb.confirm_create(candidate)
                        ),
                        InlineKeyboardButton("🎲 Random instead", callback_data=cb.GEN),
                    ]
                ]
            )
            await self._reply(update, text, markup)
            return

        await self._reply(
            update,
            fmt.notice(
                "I didn't catch a command there. Send /generate for a new alias, "
                "/otp for recent codes, or /help for the full list."
            ),
            fmt.menu_keyboard(),
        )

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None or not message.photo:
            return
        if (context.user_data or {}).get("mode") != "feedback":
            await self._reply(
                update,
                fmt.notice("To send a screenshot, start with /feedback first."),
            )
            return
        if context.user_data is not None:
            context.user_data.pop("mode", None)
            context.user_data["uid"] = user.id

        photo = message.photo[-1]  # largest rendition
        caption = message.caption or "(no caption)"
        logger.info("feedback photo from %s (%s bytes)", user.id, getattr(photo, "file_size", "?"))
        sent = await self._send_to_channel(
            context,
            user_id=user.id,
            header=self._feedback_header(user),
            body=caption,
            photo=photo,
        )
        await self._reply(
            update,
            "✅ Thanks — your screenshot reached the admin."
            if sent
            else fmt.error("Could not deliver your screenshot. Please try again later."),
        )

    # ----------------------------------------------------------------- admin
    async def ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_ban(update, context, banned=True)

    async def unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_ban(update, context, banned=False)

    async def _set_ban(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, banned: bool
    ) -> None:
        if not self._admin_only(update):
            return
        verb = "ban" if banned else "unban"
        if not context.args:
            await self._reply(update, fmt.error(f"Usage: /{verb} <user_id>"))
            return
        try:
            target = int(context.args[0])
        except ValueError:
            await self._reply(update, fmt.error("That is not a numeric user id."))
            return
        await self._db(self.db.set_banned, target, banned)
        await self._reply(
            update, fmt.notice(f"User {target} {'banned' if banned else 'unbanned'}.")
        )

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_only(update):
            return
        if not context.args:
            await self._reply(update, fmt.error("Usage: /broadcast <message>"))
            return
        payload = " ".join(context.args)
        recipients = await self._db(self.db.all_user_ids)
        sent = failed = 0
        for user_id in recipients:
            try:
                await context.bot.send_message(
                    chat_id=user_id, text=payload, parse_mode=ParseMode.HTML
                )
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 1)) + 0.5)
                try:
                    await context.bot.send_message(
                        chat_id=user_id, text=payload, parse_mode=ParseMode.HTML
                    )
                    sent += 1
                except TelegramError:
                    failed += 1
            except TelegramError as exc:
                logger.debug("broadcast to %s failed: %s", user_id, exc)
                failed += 1
            await asyncio.sleep(0.05)  # ~20 messages/second keeps us under limits
        await self._reply(
            update, fmt.notice(f"Broadcast done — {sent} delivered, {failed} failed.")
        )

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_only(update):
            return
        stats = await self._db(self.db.stats)
        text = (
            "📊 <b>Statistics</b>\n\n"
            f"👥 Users: {stats['users']} ({stats['banned']} banned)\n"
            f"📧 Aliases: {stats['aliases']} ({stats['active_aliases']} active)\n"
            f"✉️ Messages stored: {stats['messages']}\n"
            f"💬 Feedback: {stats['feedback']}"
        )
        await self._reply(update, text)

    # ------------------------------------------------------------- callbacks
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        user = query.from_user
        if context.user_data is not None:
            context.user_data["uid"] = user.id
        action, params = cb.parse(query.data or "")
        if not action:
            await query.answer("That button is no longer usable.")
            return
        if await self._db(self.db.is_banned, user.id):
            await query.answer("You are banned from using this bot.", show_alert=True)
            return
        await self._db(self.db.ensure_user, user.id)

        try:
            if query.data == cb.GEN:
                await query.answer()
                await self._create_alias(update, context, edit=True)
            elif action == "a" and params and params[0] == "otp":
                page = int(params[1]) if len(params) > 1 and params[1].isdigit() else 0
                await query.answer()
                await self._show_otps(update, page=page, edit=True)
            elif query.data == cb.HISTORY:
                await query.answer()
                aliases = await self._db(self.db.list_aliases, user.id, limit=20)
                text, markup = fmt.alias_list(self.config, aliases)
                await self._reply(update, text, markup, edit=True)
            elif action == "v":
                alias, page = cb.parse_view(query.data or "")
                if alias is None:
                    await query.answer("Malformed link.")
                    return
                if alias == "otp":
                    await query.answer()
                    await self._show_otps(update, page=page, edit=True)
                else:
                    await query.answer()
                    await self._show_alias(update, alias, page=page, edit=True)
            elif action == "c":
                await query.answer()
                await self._create_alias(update, context, requested=params[0], edit=True)
            elif action == "d":
                await query.answer()
                await self._set_active(update, params[0], False)
            elif action == "r":
                await query.answer()
                await self._set_active(update, params[0], True)
            elif action == "s":
                await query.answer()
                await self._reveal(update, int(params[0]), params[1])
            elif query.data == cb.FEEDBACK:
                await query.answer()
                await self.feedback(update, context)
            elif query.data in (cb.START, cb.HELP):
                await query.answer()
                await self._reply(
                    update,
                    fmt.welcome(self.config, max_aliases=self.config.max_aliases_per_user),
                    fmt.menu_keyboard(),
                    edit=True,
                )
            else:
                await query.answer("Unknown action.")
        except (IndexError, ValueError):
            logger.warning("unusable callback data: %r", query.data)
            await query.answer("That button is no longer usable.")
        except RetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)))

    async def _set_active(self, update: Update, alias: str, active: bool) -> None:
        user = update.effective_user
        assert user is not None
        changed = await self._db(self.db.set_alias_active, user.id, normalize(alias), active)
        if not changed:
            await self._reply(update, fmt.error(f"You have no alias called {alias}."), edit=True)
            return
        aliases = await self._db(self.db.list_aliases, user.id, limit=20)
        text, markup = fmt.alias_list(self.config, aliases)
        prefix = "♻️ Restored" if active else "🗑 Deleted"
        await self._reply(update, f"{prefix} {fmt.code(alias)}\n\n{text}", markup, edit=True)

    async def _reveal(self, update: Update, message_id: int, field: str) -> None:
        """Send the full secret. Ownership is enforced in SQL."""
        user = update.effective_user
        assert user is not None
        if field not in ("otp", "link"):
            await self._reply(update, fmt.error("Unknown field."))
            return
        value = await self._db(self.db.message_secret, user.id, message_id, field)
        if not value:
            await self._reply(
                update,
                fmt.error("That code is no longer available (it may have expired)."),
            )
            return
        label = "🔑 OTP" if field == "otp" else "🔗 Verification link"
        await self._reply(
            update,
            fmt.secret_reveal(label, value, "Tap the value to copy it."),
        )

    # ---------------------------------------------------------------- errors
    async def on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("handler error", exc_info=context.error)
        # Duck-typed: error handlers are also called with plain objects (e.g. a
        # raw Message during shutdown), and an isinstance(Update) guard silently
        # dropped the user-facing apology in those cases.
        message = getattr(update, "effective_message", None)
        if message is None:
            return
        try:
            await message.reply_text(
                fmt.error("Something went wrong. Please try again."),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:  # pragma: no cover
            pass


def build_notification(config: Config) -> Callable[[StoredMessage], Notification]:
    """Renderer handed to the notifier: one place decides what alerts look like."""

    def render(stored: StoredMessage) -> Notification:
        message = Message(
            id=stored.message_id,
            alias=stored.alias,
            subject=stored.subject,
            body=stored.preview,
            received_at=stored.received_at,
            seen=False,
            otp=stored.otp,
            links=stored.links,
        )
        if message.has_secret:
            text, markup = fmt.otp_notification(config, message)
            kind = "otp"
        else:
            text, markup = fmt.mail_notification(config, message)
            kind = "message"
        # Push alerts do not pass through BotHandlers._reply, so the credit
        # footer is applied here too: every message the user receives is branded,
        # whether it answers a command or arrives on its own.
        text = fmt.finalize(text, config)
        asset = None
        if config.send_brand_photo and config.brand_photo and Path(config.brand_photo).is_file():
            asset = str(config.brand_photo)
        return Notification(
            user_id=stored.user_id,
            text=text,
            markup=markup,
            kind=kind,
            # Photo on the alerts that matter (codes); plain mail stays text-only
            # so the chat does not turn into an image feed.
            photo=asset if kind == "otp" else None,
            caption_fallback=fmt.brand_caption(config),
        )

    return render
