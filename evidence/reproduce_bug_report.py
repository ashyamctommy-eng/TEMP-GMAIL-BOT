#!/usr/bin/env python3
"""Reproduce the original bot's bugs from its own code.

Runs against ``evidence/original_GmailBot.py`` (a byte-identical copy of the
uploaded file, md5 06599d642536b7ef9603e174f4faa4ae) and prints the BEFORE
numbers quoted in docs/IMPROVEMENTS.md. No network, no Telegram, no Gmail.

    python evidence/reproduce_bug_report.py
"""

from __future__ import annotations

import collections
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / "original_GmailBot.py"

os.environ.update(
    BOT_TOKEN="1:dummy",
    ADMIN_USER_ID="1",
    FEEDBACK_CHANNEL_ID="-100123",
    GMAIL_EMAIL="a@b.com",
    GMAIL_APP_PASSWORD="x",
)
sys.path.insert(0, str(HERE))


def load_original():
    spec = importlib.util.spec_from_file_location("original_gmailbot", ORIGINAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bug_otp_false_positive(orig) -> None:
    body = textwrap.dedent(
        """
        Your order 5691 has shipped. It will arrive at 90210 on Thursday.
        Thanks for shopping with us. Total: $49.99
        """
    )
    print("1. OTP extractor takes the first regex hit")
    print(f"   body with NO code      -> {orig.OTPService.extract_otp(body)!r}")
    print(
        "   body with a real code  -> "
        f"{orig.OTPService.extract_otp('Your verification code is 483920.')!r}"
    )


def bug_global_alias_uniqueness(orig, tmp: Path) -> None:
    orig.DB_NAME = str(tmp / "unique.db")
    db = orig.DatabaseManager()
    db.add_user(1)
    db.add_user(2)
    print("\n2. Alias names are unique across ALL users")
    print(f"   user 1 claims 'tiger123' -> {db.add_alias(1, 'tiger123')}")
    print(
        "   user 2 claims 'tiger123' -> "
        f"{db.add_alias(2, 'tiger123')}  (blocked, and reveals it exists)"
    )


def bug_concurrency(orig, tmp: Path) -> None:
    orig.DB_NAME = str(tmp / "concurrent.db")
    db = orig.DatabaseManager()
    db.add_user(1)
    db.add_alias(1, "tiger123")

    errors: list[str] = []
    per_writer = 400

    def writer(tag: str) -> None:
        try:
            for index in range(per_writer):
                db.add_message("tiger123", f"{tag}-{index}", "body")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def reader() -> None:
        try:
            for _ in range(200):
                db.get_recent_messages(1)
                time.sleep(0.0005)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=writer, args=("a",)),
        threading.Thread(target=writer, args=("b",)),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    cursor = db.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM messages")
    stored = cursor.fetchone()[0]
    cursor.execute("PRAGMA journal_mode")
    journal = cursor.fetchone()[0]
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = [row[0] for row in cursor.fetchall()]

    print("\n3. One shared SQLite connection, two threads")
    print(f"   errors: {errors[:1] or 'none'}")
    print(f"   expected {per_writer * 2} rows, stored {stored} -> LOST {per_writer * 2 - stored}")
    print(f"   journal_mode={journal!r}  indexes={indexes}")


def bug_format_labels(orig) -> None:
    labels = collections.Counter()
    for _ in range(5000):
        alias = orig.AliasGenerator.generate_random_alias()
        labels[orig.TelegramBot._detect_alias_format(None, alias)] += 1
    print("\n4. Format labels are guessed after the fact")
    print(f"   labels users see: {dict(labels.most_common(4))}")
    print(
        "   adjective+noun passes its own uniqueness rule: "
        f"{sum(orig.AliasGenerator._is_unique_enough(orig.AliasGenerator._format_adjective_noun()) for _ in range(2000)) / 2000:.0%}"
        "  <- 0%, so it always falls back to hex"
    )


def bug_html_body(orig, tmp: Path) -> None:
    import email as email_module

    orig.DB_NAME = str(tmp / "html.db")
    db = orig.DatabaseManager()
    manager = orig.GmailManager(db)
    raw = (
        b"Content-Type: text/html\r\n\r\n"
        b"<html><body><p>Code: <b>123456</b></p></body></html>"
    )
    message = email_module.message_from_bytes(raw)
    print("\n5. HTML-only mail is stored raw")
    print(f"   stored body -> {manager._extract_body(message)!r}")


def bug_text_handler(orig) -> None:
    import re

    print("\n6. Any plain word becomes an alias")
    for text in ("hi", "thanks", "hello world"):
        print(f"   {text!r:>14} -> treated as alias: {bool(re.match(r'^[a-zA-Z0-9_-]{1,20}$', text))}")


def bug_import_crash() -> None:
    """Import the original with an empty environment, in a clean directory."""
    print("\n7. Importing the file with no environment")
    script = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    script.write(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('o', r'{ORIGINAL}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "try:\n"
        "    spec.loader.exec_module(module)\n"
        "    print('imported ok')\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__, ':', exc)\n"
    )
    script.close()
    try:
        result = subprocess.run(
            [sys.executable, script.name],
            cwd=tempfile.mkdtemp(),
            capture_output=True,
            text=True,
            # Empty environment: that is the "fresh clone, no .env" situation.
            env={"PATH": os.environ.get("PATH", ""), "HOME": tempfile.mkdtemp()},
        )
        output = (result.stdout + result.stderr).strip().splitlines()
        print(f"   {output[-1] if output else '(no output)'}")
    finally:
        os.unlink(script.name)


def main() -> int:
    if not ORIGINAL.is_file():
        print(f"missing {ORIGINAL}")
        return 1
    os.chdir(tempfile.mkdtemp())  # the original writes bot.log to cwd
    orig = load_original()
    tmp = Path(tempfile.mkdtemp())

    bug_otp_false_positive(orig)
    bug_global_alias_uniqueness(orig, tmp)
    bug_concurrency(orig, tmp)
    bug_format_labels(orig)
    bug_html_body(orig, tmp)
    bug_text_handler(orig)
    bug_import_crash()
    print("\nAll of the above are covered by tests in tests/ against the refactor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
