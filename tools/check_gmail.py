#!/usr/bin/env python3
"""Check Gmail IMAP credentials and alias delivery before deploying.

    python tools/check_gmail.py            # reads .env / environment
    GMAIL_EMAIL=... GMAIL_APP_PASSWORD=... python tools/check_gmail.py

Exits 0 when login works, 1 on authentication failure, 2 on missing config.
Never prints the password -- only the address, host and what it found.
"""

from __future__ import annotations

import email
import imaplib
import os
import sys
from datetime import date, timedelta
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmailbot.config import Config, ConfigError, load_dotenv  # noqa: E402

OK = "  ok  "
BAD = " fail "


def decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def env_or_dotenv() -> dict[str, str]:
    values = load_dotenv(ROOT / ".env")
    return {**values, **os.environ}


def main() -> int:
    env = env_or_dotenv()
    try:
        config = Config.from_env(env, root=ROOT)
    except ConfigError as exc:
        print(f"[{BAD}] configuration\n{exc}\n")
        return 2

    print(f"[{OK}] config loaded")
    print(f"       GMAIL_EMAIL   : {config.gmail_email}")
    print(f"       alias pattern : {config.alias_address.replace('{alias}', '<alias>')}")
    print(f"       mailbox       : {config.imap_mailbox} (read-only)")
    print(f"       imap host     : {config.imap_host}:993")

    try:
        client = imaplib.IMAP4_SSL(config.imap_host, 993, timeout=20)
    except OSError as exc:
        print(f"[{BAD}] cannot reach {config.imap_host}:993 -- {exc}")
        print("       Outbound port 993 may be blocked (common on shared hosting).")
        return 1

    try:
        client.login(config.gmail_email, config.gmail_app_password)
    except imaplib.IMAP4.error as exc:
        message = " ".join(str(exc).split())
        print(f"[{BAD}] login rejected -- {message}")
        print("       Fix: use a 16-character App Password, not the account password.")
        print("       See docs/GMAIL_SETUP.md (2-Step Verification must be ON first).")
        return 1
    print(f"[{OK}] login accepted")

    status, data = client.select(config.imap_mailbox, readonly=True)
    if status != "OK":
        print(f"[{BAD}] cannot open mailbox {config.imap_mailbox!r}: {data}")
        print("       Is IMAP enabled in Gmail settings? Only IMAP matters, POP does not.")
        return 1
    total = int(data[0]) if data and data[0].isdigit() else 0
    print(f"[{OK}] mailbox opened (read-only) -- {total} message(s)")

    # Look for alias mail from the last few days, so a silent alias mismatch shows up.
    since = (date.today() - timedelta(days=7)).strftime("%d-%b-%Y")
    status, data = client.uid("SEARCH", None, "SINCE", since)
    uids = data[0].split() if status == "OK" and data and data[0] else []
    matches: list[tuple[str, str, str]] = []
    for uid in uids[-40:]:
        status, payload = client.uid("FETCH", uid.decode(), "(BODY.PEEK[HEADER])")
        if status != "OK":
            continue
        raw = b""
        for part in payload or []:
            if isinstance(part, tuple) and isinstance(part[1], bytes):
                raw = part[1]
                break
        if not raw:
            continue
        message = email.message_from_bytes(raw)
        recipients = " ".join(
            str(message.get(header) or "")
            for header in ("To", "Delivered-To", "X-Original-To", "Cc")
        )
        needle = f"{config.alias_root}+"
        if needle.lower() in recipients.lower():
            subject = decode(message.get("Subject")) or "(no subject)"
            matches.append((str(message.get("Date", ""))[:31], subject, recipients.strip()))

    if matches:
        print(f"[{OK}] {len(matches)} alias message(s) in the last 7 days")
        for stamp, subject, recipient in matches[-5:]:
            print(f"       {stamp[:17]}  {subject[:48]!r} -> {recipient[:48]}")
    else:
        print(f"[!] no mail to {config.alias_root}+<alias>@ in the last 7 days")
        print("       Send yourself a test: send an email TO one of your aliases,")
        print("       or check that alias mail is not being filtered out of")
        print(f"       {config.imap_mailbox!r} (see docs/GMAIL_SETUP.md).")

    client.logout()
    print(f"\n[{OK}] all good -- start the bot with: python run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
