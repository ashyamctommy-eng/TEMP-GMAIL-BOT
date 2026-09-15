"""Application wiring: the only place that constructs concrete services."""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application

from .ai import make_openrouter_lookup
from .aliases import AliasGenerator
from .config import Config, setup_logging
from .db import Database
from .handlers import BotHandlers, ChannelGuard, build_notification
from .mail import GmailPoller
from .notify import Notifier
from .otp import OtpExtractor

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("generate", "Create a new alias"),
    BotCommand("otp", "Recent codes & verification links"),
    BotCommand("history", "Your aliases"),
    BotCommand("view", "Messages for one alias"),
    BotCommand("delete", "Deactivate an alias"),
    BotCommand("feedback", "Message the admin"),
    BotCommand("help", "How this bot works"),
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
    )
    handlers = BotHandlers(
        config,
        db,
        generator=generator,
        extractor=extractor,
        notifier=notifier,
        guard=ChannelGuard(config.feedback_channel_id),
    )

    async def post_init(application: Application) -> None:
        try:
            await application.bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
            logger.warning("could not set command menu: %s", exc)
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
