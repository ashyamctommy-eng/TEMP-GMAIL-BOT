"""Config must fail loudly, with actionable messages, and resolve paths itself."""

from __future__ import annotations

import pytest

from gmailbot.config import Config, ConfigError, load_dotenv
from tests.conftest import BASE_ENV


def test_missing_values_are_all_reported_at_once(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({}, root=tmp_path)
    message = str(excinfo.value)
    for key in ("BOT_TOKEN", "GMAIL_EMAIL", "GMAIL_APP_PASSWORD", "ADMIN_USER_ID", "FEEDBACK_CHANNEL_ID"):
        assert key in message


def test_placeholder_values_are_rejected(tmp_path):
    """The original shipped `int(os.getenv("ADMIN_USER_ID", "Enter_your_admin_id_here"))`,
    which raised `ValueError: invalid literal for int()` at import time."""
    env = {**BASE_ENV, "ADMIN_USER_ID": "Enter_your_admin_id_here"}
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env, root=tmp_path)
    assert "ADMIN_USER_ID" in str(excinfo.value)


def test_bot_token_shape_is_validated(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "BOT_TOKEN": "not-a-token"}, root=tmp_path)


def test_gmail_app_password_shape_is_validated(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**BASE_ENV, "GMAIL_APP_PASSWORD": "hunter2"}, root=tmp_path)
    assert "App Password" in str(excinfo.value)


def test_spaced_app_password_is_accepted_and_normalised(env, tmp_path):
    """Google displays it as 'abcd efgh ijkl mnop'; both paste styles must work."""
    spaced = Config.from_env({**env, "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}, root=tmp_path)
    plain = Config.from_env({**env, "GMAIL_APP_PASSWORD": "abcdefghijklmnop"}, root=tmp_path)
    assert spaced.gmail_app_password == "abcdefghijklmnop"
    assert spaced.gmail_app_password == plain.gmail_app_password


def test_wrong_length_app_password_is_rejected_with_a_useful_message(env, tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**env, "GMAIL_APP_PASSWORD": "abcd efgh ij"}, root=tmp_path)
    message = str(excinfo.value)
    # "abcd efgh ij" -> 10 non-space characters; the message must say so.
    assert "10 non-space characters" in message


def test_alias_root_and_domain_derive_from_the_mailbox(env, tmp_path):
    config = Config.from_env(env, root=tmp_path)
    assert config.alias_root == "owner"
    assert config.alias_domain == "gmail.com"
    assert config.full_alias("abc") == "owner+abc@gmail.com"
    assert config.alias_address == "owner+{alias}@gmail.com"


def test_paths_resolve_against_the_project_root_not_cwd(env, tmp_path):
    """The original used os.getcwd(), so the DB moved with the working directory."""
    relative = {**env, "DB_PATH": "data/bot.db", "LOG_PATH": "logs/bot.log"}
    config = Config.from_env(relative, root=tmp_path)
    assert config.db_path == (tmp_path / "data" / "bot.db").resolve()
    assert config.log_path == (tmp_path / "logs" / "bot.log").resolve()


def test_defaults_are_sane(env, tmp_path):
    config = Config.from_env(env, root=tmp_path)
    assert config.message_ttl_seconds == 3600
    assert config.poll_interval_seconds == 10
    assert config.use_ai_otp_fallback is False
    assert config.openrouter_api_key is None


def test_non_positive_numbers_are_rejected(env, tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({**env, "MESSAGE_TTL_SECONDS": "0"}, root=tmp_path)


def test_dotenv_parsing_ignores_comments_and_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "\n"
        "BOT_TOKEN=123:abc\n"
        'GMAIL_EMAIL="someone@gmail.com"\n'
        "WEIRD=value=with=equals\n"
    )
    values = load_dotenv(path)
    assert values["BOT_TOKEN"] == "123:abc"
    assert values["GMAIL_EMAIL"] == "someone@gmail.com"
    assert values["WEIRD"] == "value=with=equals"


def test_dotenv_is_optional(tmp_path):
    assert load_dotenv(tmp_path / "missing") == {}


def test_dev_url_is_configurable(env):
    assert Config.from_env(env).dev_url == "https://t.me/Poriot_ke"
    custom = Config.from_env({**env, "DEV_URL": "https://t.me/n_ke"})
    assert custom.dev_url == "https://t.me/n_ke"
