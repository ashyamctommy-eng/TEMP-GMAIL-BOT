"""Application wiring: the only place that constructs concrete services."""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import Application, TypeHandler

from .ai import make_link_judge, make_openrouter_lookup
from .aliases import AliasGenerator
from .config import Config, setup_logging
from .db import Database
from .handlers import BotHandlers, ChannelGuard, build_notification
from .membership import MembershipGate, normalize_channel_ref
from .mail import GmailPoller
from .notify import Notifier
from .otp import OtpExtractor

logger = logging.getLogger(__name__)

#: Pushed to Telegram on every start, so users get the command menu with no
#: manual BotFather setup. Telegram shows these in the "/" menu and the blue
#: Menu button automatically.
COMMANDS = [
    BotCommand("start", "Show the menu"),
    BotCommand("generate", "Create a new alias"),
    BotCommand("otp", "Recent codes & verification links"),
    BotCommand("history", "Your aliases"),
    BotCommand("view", "Messages for one alias"),
    BotCommand("delete", "Deactivate an alias"),
    BotCommand("feedback", "Message the admin"),
    BotCommand("cancel", "Leave feedback mode"),
    BotCommand("help", "How this bot works"),
]

#: Admin commands are registered for the owner only, so they do not clutter (or
#: advertise themselves in) everybody else's menu.
ADMIN_COMMANDS = COMMANDS + [
    BotCommand("admin", "Admin panel"),
    BotCommand("stats", "Usage statistics"),
    BotCommand("channels", "Required channels"),
    BotCommand("addchannel", "Require a channel to use the bot"),
    BotCommand("delchannel", "Stop requiring a channel"),
    BotCommand("trackers", "Learned click-wrapper patterns"),
    BotCommand("ban", "Ban a user"),
    BotCommand("unban", "Unban a user"),
    BotCommand("broadcast", "Message every user"),
]


def build_application(config: Config) -> Application:
    """Compose services and handlers.

    Everything is constructor-injected, which is what makes the test suite
    possible without a Telegram token, a Gmail account or a network.
    """
    db = Database(config.db_path)

    generator = AliasGenerator(
        taken=lambda name: db.alias_owner(name) is not None,
    )
    extractor = OtpExtractor(
        ai_lookup=(
            make_openrouter_lookup(config.openrouter_api_key)
            if config.use_ai_otp_fallback
            else None
        )
    )
    notifier = Notifier(build_notification(config))
    poller = GmailPoller(
        config,
        db,
        on_message=notifier.emit,
        extractor=extractor,
        # Teacher, not runtime dependency: used only on ambiguous links, and only
        # to learn a host pattern the deterministic rules apply from then on.
        judge=(
            make_link_judge(config.openrouter_api_key, model=config.link_judge_model)
            if config.use_ai_link_fallback
            else None
        ),
    )
    gate = MembershipGate(
        db,
        enabled=config.force_join_enabled,
        cache_seconds=config.membership_cache_seconds,
        admin_user_id=config.admin_user_id,
    )
    handlers = BotHandlers(
        config,
        db,
        generator=generator,
        extractor=extractor,
        notifier=notifier,
        guard=ChannelGuard(config.feedback_channel_id),
        gate=gate,
    )
    # Deploy-time seed; /addchannel and /delchannel manage it at runtime after this.
    for raw in config.required_channels_seed:
        ref = normalize_channel_ref(raw)
        if ref is None:
            logger.error(
                "REQUIRED_CHANNELS: %r is not a channel handle or id — use "
                "@username or -100... ; ignoring it",
                raw,
            )
            continue
        db.add_required_channel(ref, ref, None, config.admin_user_id)

    async def post_init(application: Application) -> None:
        # Everything a user sees before typing anything is registered here, so a
        # fresh deployment needs no manual configuration in @BotFather.
        try:
            await application.bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
            logger.warning("could not set command menu: %s", exc)
        try:
            await application.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=config.admin_user_id)
            )
        except Exception as exc:  # noqa: BLE001
            # Expected until the owner has pressed /start at least once.
            logger.info("could not set the admin command menu: %s", exc)
        try:
            await application.bot.set_my_short_description(
                f"{config.brand_name} — Gmail aliases with instant OTP codes."
            )
            await application.bot.set_my_description(
                f"{config.brand_name}\n\n"
                "Get a fresh Gmail alias for any signup and receive the mail, OTP "
                "codes and verification links right here in Telegram.\n\n"
                f"{config.credit_line}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not set bot description: %s", exc)
        # Resolve the required channels once at boot: fills in their titles and
        # invite links, and shouts in the log if one cannot be read (the gate
        # fails open, so a silent misconfiguration would disable it entirely).
        for channel in db.list_required_channels():
            try:
                chat = await application.bot.get_chat(channel.chat_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "required channel %s cannot be resolved (%s) — users will NOT be "
                    "gated until this is fixed",
                    channel.chat_id,
                    exc,
                )
                continue
            link = chat.invite_link or (
                f"https://t.me/{chat.username}" if chat.username else channel.invite_link
            )
            db.add_required_channel(
                channel.chat_id, chat.title or channel.title, link, config.admin_user_id
            )

        notifier.bind(asyncio.get_running_loop(), application)
        notifier.start()
        poller.start()
        logger.info("bot ready")

    async def post_shutdown(application: Application) -> None:
        # Graceful shutdown matters on Plesk/systemd restarts: an open IMAP
        # session and an uncommitted WAL left behind used to be the norm.
        poller.stop()
        await notifier.stop()
        db.close()
        logger.info("shutdown complete")

    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(application)
    # Runs before every other handler (group -1) and stops non-members there.
    application.add_handler(TypeHandler(Update, gate.middleware), group=-1)
    application.bot_data["db"] = db
    application.bot_data["poller"] = poller
    return application


def main() -> int:
    """Entry point. Run this as a long-lived process."""
    try:
        config = Config.from_env()
    except Exception as exc:  # ConfigError carries the actionable message
        print(f"\n{exc}\n", flush=True)
        return 2

    setup_logging(config)
    logger.info("starting gmailbot (db=%s)", config.db_path)
    application = build_application(config)
    # run_polling installs SIGINT/SIGTERM handlers and runs post_shutdown for us.
    application.run_polling(drop_pending_updates=True)
    return 0
