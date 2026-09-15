from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmailbot.aliases import AliasGenerator  # noqa: E402
from gmailbot.config import Config  # noqa: E402
from gmailbot.db import Database  # noqa: E402
from gmailbot.notify import Notifier  # noqa: E402
from gmailbot.otp import OtpExtractor  # noqa: E402

BASE_ENV = {
    "BOT_TOKEN": "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    "GMAIL_EMAIL": "owner@gmail.com",
    "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
    "ADMIN_USER_ID": "42",
    "FEEDBACK_CHANNEL_ID": "-1001234567890",
}


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {**BASE_ENV, "DB_PATH": str(tmp_path / "bot.db"), "LOG_PATH": str(tmp_path / "bot.log")}


@pytest.fixture
def config(env, tmp_path) -> Config:
    return Config.from_env(env, root=tmp_path)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def generator() -> AliasGenerator:
    import random

    return AliasGenerator(rng=random.Random(1234))


@pytest.fixture
def extractor() -> OtpExtractor:
    return OtpExtractor()


@pytest.fixture
def notifier() -> Notifier:
    return Notifier(lambda stored: None)  # type: ignore[arg-type]
