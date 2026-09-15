"""Deployment wiring: what users see without the owner configuring anything."""

from __future__ import annotations

from pathlib import Path

from telegram.ext import CommandHandler

from gmailbot.app import ADMIN_COMMANDS, COMMANDS, build_application
from gmailbot.formatting import CAPTION_LIMIT, TELEGRAM_TEXT_LIMIT, utf16_len

TELEGRAM_COMMAND_LIMIT = 32
TELEGRAM_DESCRIPTION_LIMIT = 512
TELEGRAM_SHORT_DESCRIPTION_LIMIT = 120


def registered_commands(application) -> set[str]:
    found: set[str] = set()
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                found.update(handler.commands)
    return found


def test_every_command_is_advertised_in_the_menu(config):
    """The bot registers its own command menu, so no BotFather setup is needed."""
    application = build_application(config)
    advertised = {command.command for command in ADMIN_COMMANDS}
    registered = registered_commands(application)
    assert registered, "no command handlers registered"
    assert registered <= advertised, f"missing from the menu: {registered - advertised}"


def test_admin_commands_are_not_in_the_public_menu():
    public = {command.command for command in COMMANDS}
    assert {"ban", "unban", "broadcast", "stats"}.isdisjoint(public)
    assert {"generate", "otp", "help", "start"} <= public


def test_menu_entries_respect_telegram_limits():
    for command in COMMANDS:
        assert command.command.islower()
        assert " " not in command.command
        assert len(command.command) <= TELEGRAM_COMMAND_LIMIT
        assert 0 < len(command.description) <= 256
    assert len({command.command for command in COMMANDS}) == len(COMMANDS)


def test_descriptions_fit_telegram_limits(config):
    from gmailbot.app import build_application  # local import keeps the test honest

    application = build_application(config)
    assert application is not None
    short = f"{config.brand_name} — Gmail aliases with instant OTP codes."
    long = (
        f"{config.brand_name}\n\n"
        "Get a fresh Gmail alias for any signup and receive the mail, OTP codes "
        "and verification links right here in Telegram.\n\n"
        f"{config.credit_line}"
    )
    assert utf16_len(short) <= TELEGRAM_SHORT_DESCRIPTION_LIMIT
    assert utf16_len(long) <= TELEGRAM_DESCRIPTION_LIMIT


def test_brand_photo_asset_ships_with_the_package(config):
    """The default photo must exist in the repo, not only on the author's machine."""
    assert config.brand_photo is not None
    assert Path(config.brand_photo).is_file()
    assert Path(config.brand_photo).suffix in {".jpg", ".jpeg", ".png"}


def test_brand_photo_can_be_disabled_via_env(env, tmp_path):
    from gmailbot.config import Config

    disabled = Config.from_env({**env, "BOT_PHOTO_PATH": "none"}, root=tmp_path)
    assert disabled.brand_photo is None


def test_bad_photo_path_is_reported_not_ignored(env, tmp_path):
    from gmailbot.config import Config, ConfigError

    try:
        Config.from_env({**env, "BOT_PHOTO_PATH": "does/not/exist.png"}, root=tmp_path)
    except ConfigError as exc:
        assert "BOT_PHOTO_PATH" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a missing photo path should be reported at boot")


def test_brand_can_be_overridden_without_touching_code(env, tmp_path):
    from gmailbot.config import Config

    custom = Config.from_env(
        {**env, "BOT_BRAND_NAME": "Plain Name", "BOT_CREDIT": "someone"}, root=tmp_path
    )
    assert custom.brand_name == "Plain Name"
    assert custom.credit_line == "Bot by: someone"


def test_limits_are_measured_in_utf16_units():
    """Styled fonts and emoji cost two UTF-16 units each, as Telegram counts them."""
    styled = "𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕"
    assert len(styled) == 14          # code points
    assert utf16_len(styled) == 26    # what Telegram counts
    assert CAPTION_LIMIT < TELEGRAM_TEXT_LIMIT
