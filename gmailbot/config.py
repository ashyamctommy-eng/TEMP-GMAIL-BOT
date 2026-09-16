"""Configuration loading.

Design rules (see docs/IMPROVEMENTS.md, P0-1):

* Never ship placeholder defaults for secrets. Missing config must fail loudly
  at startup with *all* problems listed at once, not raise a bare
  ``ValueError: invalid literal for int()`` from an import statement.
* Paths are resolved relative to the project root, never ``os.getcwd()``, so
  the process can be started from anywhere (Plesk, systemd, cron, tests).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Brand strings. These are Unicode "styled" fonts (Mathematical Alphanumeric
#: Symbols): they are ordinary characters, so any client *font* can fail to
#: render them (they show as boxes on some Android keyboards/fonts). They are
#: config, not code, so they can be swapped for plain text in one place.
BRAND_NAME_DEFAULT = "𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕"
BRAND_CREDIT_DEFAULT = "𝙋𝙤𝙧𝙞𝙤𝙩_𝙠𝙚"
BRAND_PHOTO_DEFAULT = PROJECT_ROOT / "gmailbot" / "assets" / "tempgmail.jpg"

#: A Telegram bot token looks like ``123456789:AA...``
_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed config."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        bullets = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            "Invalid configuration:\n"
            f"{bullets}\n\n"
            "Copy .env.example to .env and fill it in, or export these variables "
            "in your process manager."
        )


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (no dependency on python-dotenv).

    Returns the parsed values; it does not touch ``os.environ`` so callers stay
    in control of precedence.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _get(
    env: Mapping[str, str],
    key: str,
    problems: list[str],
    *,
    required: bool = True,
    default: str | None = None,
) -> str | None:
    value = env.get(key)
    if value is None or not str(value).strip():
        if required:
            problems.append(f"{key} is required but not set")
            return None
        return default
    return str(value).strip()


def _get_int(
    env: Mapping[str, str],
    key: str,
    problems: list[str],
    *,
    required: bool = True,
    default: int | None = None,
) -> int | None:
    raw = _get(env, key, problems, required=required)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        problems.append(f"{key} must be an integer, got {raw!r}")
        return default


@dataclass(frozen=True)
class Config:
    """Validated, immutable runtime configuration."""

    bot_token: str
    gmail_email: str
    gmail_app_password: str
    admin_user_id: int
    feedback_channel_id: int
    db_path: Path
    log_path: Path
    alias_root: str
    alias_domain: str
    message_ttl_seconds: int = 3600
    poll_interval_seconds: int = 10
    imap_host: str = "imap.gmail.com"
    #: Folder the poller reads. Gmail users who auto-archive alias mail can point
    #: this at "[Gmail]/All Mail" so filtered messages are still picked up.
    imap_mailbox: str = "INBOX"
    initial_lookback_days: int = 1
    max_aliases_per_user: int = 25
    generate_per_hour: int = 20
    openrouter_api_key: str | None = None
    use_ai_otp_fallback: bool = False
    #: Ask the AI to classify ambiguous links and LEARN the wrapper host pattern,
    #: so later mail is filtered offline. Only redacted URLs are ever sent.
    use_ai_link_fallback: bool = False
    link_judge_model: str = "openai/gpt-4o-mini"
    log_level: str = "INFO"
    brand_name: str = BRAND_NAME_DEFAULT
    brand_credit: str = BRAND_CREDIT_DEFAULT
    brand_photo: Path | None = BRAND_PHOTO_DEFAULT
    send_brand_photo: bool = True
    #: Require users to join the configured channels before the bot responds.
    #: Render the bot's own headings/labels in the brand font. Off = plain text.
    styled_font: bool = True
    force_join_enabled: bool = True
    #: How long a membership result is trusted (protects the Bot API rate limit).
    membership_cache_seconds: int = 300
    #: Optional deploy-time seed for the channel list (admins can also manage it).
    required_channels_seed: tuple[str, ...] = ()

    # ---------------------------------------------------------------- helpers
    @property
    def alias_address(self) -> str:
        """``user+alias@domain`` prefix shown to users (``{alias}`` placeholder)."""
        return f"{self.alias_root}+{{alias}}@{self.alias_domain}"

    def full_alias(self, alias: str) -> str:
        return f"{self.alias_root}+{alias}@{self.alias_domain}"

    @property
    def credit_line(self) -> str:
        """Appended to every user-facing message."""
        return f"Bot by: {self.brand_credit}"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        root: Path | None = None,
        dotenv: bool | None = None,
    ) -> "Config":
        root = (root or PROJECT_ROOT).resolve()
        if env is None:
            env = dict(os.environ)
            if dotenv is None:
                dotenv = True
            if dotenv:
                # Real environment variables win over .env values.
                file_values = load_dotenv(root / ".env")
                env = {**file_values, **env}
        else:
            env = dict(env)

        problems: list[str] = []

        token = _get(env, "BOT_TOKEN", problems)
        if token and not _TOKEN_RE.match(token):
            problems.append(
                "BOT_TOKEN does not look like a Telegram token "
                "(expected '123456789:AA...'); get one from @BotFather"
            )

        gmail_email = _get(env, "GMAIL_EMAIL", problems)
        gmail_password = _get(env, "GMAIL_APP_PASSWORD", problems)
        if gmail_password:
            # Google shows App Passwords as four groups of four ("abcd efgh ijkl
            # mnop"). Pasting them verbatim is the normal case, so normalise here
            # and send the bare 16 characters to IMAP: validation *and* use must
            # agree, otherwise a correct pasted password is rejected at login.
            gmail_password = re.sub(r"\s+", "", gmail_password)
        if gmail_password and len(gmail_password) != 16:
            problems.append(
                "GMAIL_APP_PASSWORD should be a 16-character Google App Password "
                "(not your account password). Spaces are fine; it has "
                f"{len(gmail_password)} non-space characters."
            )

        admin_id = _get_int(env, "ADMIN_USER_ID", problems)
        channel_id = _get_int(env, "FEEDBACK_CHANNEL_ID", problems)
        if channel_id == 0:
            problems.append("FEEDBACK_CHANNEL_ID is not a valid chat id")

        db_raw = _get(env, "DB_PATH", problems, required=False, default=None)
        db_path = Path(db_raw).expanduser() if db_raw else root / "data" / "bot.db"
        if not db_path.is_absolute():
            db_path = (root / db_path).resolve()

        log_raw = _get(env, "LOG_PATH", problems, required=False, default=None)
        log_path = Path(log_raw).expanduser() if log_raw else root / "data" / "bot.log"
        if not log_path.is_absolute():
            log_path = (root / log_path).resolve()

        ttl = _get_int(
            env, "MESSAGE_TTL_SECONDS", problems, required=False, default=3600
        )
        poll_interval = _get_int(
            env, "POLL_INTERVAL_SECONDS", problems, required=False, default=10
        )
        lookback = _get_int(
            env, "INITIAL_LOOKBACK_DAYS", problems, required=False, default=1
        )
        max_aliases = _get_int(
            env, "MAX_ALIASES_PER_USER", problems, required=False, default=25
        )
        gen_per_hour = _get_int(
            env, "GENERATE_PER_HOUR", problems, required=False, default=20
        )
        membership_ttl = _get_int(
            env, "MEMBERSHIP_CACHE_SECONDS", problems, required=False, default=300
        )
        for name, value in (
            ("MESSAGE_TTL_SECONDS", ttl),
            ("POLL_INTERVAL_SECONDS", poll_interval),
            ("INITIAL_LOOKBACK_DAYS", lookback),
            ("MAX_ALIASES_PER_USER", max_aliases),
            ("GENERATE_PER_HOUR", gen_per_hour),
            ("MEMBERSHIP_CACHE_SECONDS", membership_ttl),
        ):
            if value is not None and value <= 0:
                problems.append(f"{name} must be > 0, got {value}")

        alias_root = _get(env, "ALIAS_ROOT", problems, required=False) or None
        alias_domain = _get(env, "ALIAS_DOMAIN", problems, required=False) or None
        if gmail_email and "@" in gmail_email:
            local, _, domain = gmail_email.partition("@")
            alias_root = alias_root or local
            alias_domain = alias_domain or domain
        elif gmail_email:
            problems.append(f"GMAIL_EMAIL must look like an address, got {gmail_email!r}")

        # Aliases are matched against inbound To/Delivered-To headers; a wrong
        # root silently means "no mail ever arrives", so validate hard.
        if alias_root and not re.fullmatch(r"[A-Za-z0-9._-]+", alias_root):
            problems.append(f"ALIAS_ROOT has unsupported characters: {alias_root!r}")

        # Computed before the check below, otherwise a typo in BOT_PHOTO_PATH
        # collects a "problem" that nobody ever reads.
        brand_photo = _brand_photo(env, problems, root)

        if problems:
            raise ConfigError(problems)

        assert token and gmail_email and gmail_password  # guaranteed above
        assert admin_id is not None and channel_id is not None
        return cls(
            bot_token=token,
            gmail_email=gmail_email,
            gmail_app_password=gmail_password,
            admin_user_id=admin_id,
            feedback_channel_id=channel_id,
            db_path=db_path,
            log_path=log_path,
            alias_root=alias_root or "",
            alias_domain=alias_domain or "gmail.com",
            message_ttl_seconds=ttl or 3600,
            poll_interval_seconds=poll_interval or 10,
            imap_host=env.get("IMAP_HOST", "imap.gmail.com"),
            imap_mailbox=env.get("IMAP_MAILBOX", "INBOX") or "INBOX",
            initial_lookback_days=lookback or 1,
            max_aliases_per_user=max_aliases or 25,
            generate_per_hour=gen_per_hour or 20,
            openrouter_api_key=(
                _get(env, "OPENROUTER_API_KEY", problems, required=False) or None
            ),
            use_ai_otp_fallback=str(
                env.get("USE_AI_OTP_FALLBACK", "false")
            ).lower() in {"1", "true", "yes", "on"},
            use_ai_link_fallback=str(
                env.get("USE_AI_LINK_FALLBACK", "false")
            ).lower() in {"1", "true", "yes", "on"},
            link_judge_model=str(
                env.get("LINK_JUDGE_MODEL", "openai/gpt-4o-mini")
            ),
            log_level=str(env.get("LOG_LEVEL", "INFO")).upper(),
            brand_name=_get(
                env, "BOT_BRAND_NAME", problems, required=False,
                default=BRAND_NAME_DEFAULT,
            ) or BRAND_NAME_DEFAULT,
            brand_credit=_get(
                env, "BOT_CREDIT", problems, required=False,
                default=BRAND_CREDIT_DEFAULT,
            ) or BRAND_CREDIT_DEFAULT,
            brand_photo=brand_photo,
            send_brand_photo=str(
                env.get("SEND_BRAND_PHOTO", "true")
            ).lower() in {"1", "true", "yes", "on"},
            styled_font=str(
                env.get("STYLED_FONT", "true")
            ).lower() in {"1", "true", "yes", "on"},
            force_join_enabled=str(
                env.get("FORCE_JOIN", "true")
            ).lower() in {"1", "true", "yes", "on"},
            membership_cache_seconds=membership_ttl or 300,
            required_channels_seed=tuple(
                item.strip()
                for item in str(env.get("REQUIRED_CHANNELS", "")).split(",")
                if item.strip()
            ),
        )


def _brand_photo(env: Mapping[str, str], problems: list[str], root: Path) -> Path | None:
    """Asset used as the photo on /start and OTP alerts.

    Missing asset is a soft failure (the bot sends text instead), a *broken*
    setting is not: pointing BOT_PHOTO_PATH at something unreadable is a typo
    worth surfacing at boot.
    """
    raw = _get(env, "BOT_PHOTO_PATH", problems, required=False, default=None)
    if raw and raw.lower() in {"none", "off", "false"}:
        return None
    if not raw:
        return BRAND_PHOTO_DEFAULT if BRAND_PHOTO_DEFAULT.is_file() else None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    if not candidate.is_file():
        problems.append(f"BOT_PHOTO_PATH does not exist: {candidate}")
        return None
    return candidate


def setup_logging(config: Config) -> None:
    """Rotating file log + stderr, resolved against the project root."""
    import logging
    from logging.handlers import RotatingFileHandler

    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(config.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler = RotatingFileHandler(
        config.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
