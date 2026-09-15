"""IMAP parsing + polling.

Fixes over the original (docs/IMPROVEMENTS.md):

* P0-5  The original searched ``UNSEEN`` and only wrote ``\\Seen`` back for
  messages that matched an alias, so every unrelated UNSEEN message was
  re-fetched every 5 seconds forever.
* P0-6  Because it depended on ``\\Seen``, any mail the user opened on their
  phone first was invisible to the bot -- the single most damaging behaviour
  for an OTP inbox. We now track the last processed **UID** in the database and
  open the mailbox ``readonly=True``, so we never touch the user's unread
  state and never miss a message.
* P0-7  Parsing/OTP/link extraction now happens in the poller thread only; the
  blocking AI fallback call is removed from this path (it paused polling for up
  to 30s per message).
* P1-4  The cursor advances per message, after it is committed, so a crash
  mid-cycle cannot lose mail and cannot duplicate it either (Message-ID dedup).
"""

from __future__ import annotations

import email
import imaplib
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from email.header import decode_header
from email.message import Message as EmailMessage
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, Protocol

from .config import Config
from .db import Database
from .htmltext import html_to_text
from .links import extract_link_urls
from .models import StoredMessage, to_iso, utcnow
from .otp import OtpExtractor

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 20_000
MAX_FETCH_PER_CYCLE = 50
_RECIPIENT_HEADERS = (
    "To", "Cc", "Delivered-To", "X-Original-To", "X-Delivered-To", "Envelope-To",
)


class ImapClient(Protocol):  # pragma: no cover - structural typing only
    def login(self, user: str, password: str) -> tuple[str, list[bytes]]: ...
    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]: ...
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]: ...
    def close(self) -> tuple[str, list[bytes]]: ...
    def logout(self) -> tuple[str, list[bytes]]: ...


def default_client_factory(host: str, timeout: float = 30.0) -> ImapClient:
    return imaplib.IMAP4_SSL(host, 993, timeout=timeout)  # type: ignore[return-value]


# --------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class ParsedEmail:
    rfc_message_id: str | None
    sender: str
    recipients: tuple[str, ...]
    subject: str
    text: str
    links: list[str] = field(default_factory=list)
    received_at: str | None = None

    def recipient_blob(self) -> str:
        return " ".join(self.recipients)


def decode_mime(value: str | None) -> str:
    """Decode RFC2047 headers. Unknown charsets degrade to replacement chars."""
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            for encoding in (charset, "utf-8", "latin-1"):
                if not encoding:
                    continue
                try:
                    parts.append(chunk.decode(encoding, errors="strict"))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def extract_bodies(message: EmailMessage) -> tuple[str, str]:
    """Return ``(plain_text, html)`` with plain text preferred."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts: Iterable[EmailMessage] = (
        message.walk() if message.is_multipart() else [message]
    )
    for part in parts:
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            if not isinstance(raw, str):
                continue
            text = raw
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
        (plain_parts if content_type == "text/plain" else html_parts).append(text)
    return "\n".join(plain_parts), "\n".join(html_parts)


def parse_email(raw: bytes) -> ParsedEmail:
    message = email.message_from_bytes(raw)
    plain, html = extract_bodies(message)

    text = plain.strip()
    if not text and html:
        text = html_to_text(html)
    elif html and len(text) < 40:
        # HTML often carries the code in a picture-button the plain part omits.
        text = f"{text}\n{html_to_text(html)}".strip()
    text = text[:MAX_BODY_CHARS]

    recipients: list[str] = []
    for header in _RECIPIENT_HEADERS:
        for value in message.get_all(header, []):
            recipients.extend(part.strip() for part in str(value).split(",") if part.strip())

    received = None
    try:
        date_header = message.get("Date")
        if date_header:
            received = to_iso(parsedate_to_datetime(date_header))
    except (TypeError, ValueError):
        received = None

    return ParsedEmail(
        rfc_message_id=(message.get("Message-ID") or "").strip() or None,
        sender=decode_mime(message.get("From")),
        recipients=tuple(recipients),
        subject=decode_mime(message.get("Subject")) or "(no subject)",
        text=text,
        received_at=received,
    )


def match_alias(parsed: ParsedEmail, config: Config) -> tuple[str | None, str | None]:
    """Find ``root+alias@...`` in the recipient headers.

    Returns ``(alias, recipient)``. The domain is intentionally not pinned: the
    mailbox we authenticated into is the only source of these messages, and
    Gmail also delivers to ``googlemail.com``.
    """
    # Lookbehind rather than a "must be preceded by a space" class: real To:
    # headers arrive as `"Acme" <owner+tag@googlemail.com>, ...`.
    pattern = re.compile(
        rf"(?<![A-Za-z0-9._+\-]){re.escape(config.alias_root)}\+([^@\s,;<>\"']+)@",
        re.IGNORECASE,
    )
    for recipient in parsed.recipients:
        match = pattern.search(recipient)
        if match:
            return match.group(1).lower(), recipient
    return None, None


# --------------------------------------------------------------------- polling
class GmailPoller:
    """Background IMAP poller.

    ``on_message`` is called (from the poller thread) for each newly stored
    message. The callback must be thread-safe; the notifier handles the
    thread -> event-loop hand-off.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        on_message: Callable[[StoredMessage], None] | None = None,
        *,
        client_factory: Callable[[str], ImapClient] | None = None,
        extractor: OtpExtractor | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.on_message = on_message
        self._client_factory = client_factory or (
            lambda host: default_client_factory(host)
        )
        self._extractor = extractor or OtpExtractor()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._client: ImapClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="gmail-poller", daemon=True
        )
        self._thread.start()
        logger.info("gmail poller started (interval=%ss)", self.config.poll_interval_seconds)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._disconnect()
        logger.info("gmail poller stopped")

    # --------------------------------------------------------------- client
    def _connect(self) -> ImapClient:
        client = self._client_factory(self.config.imap_host)
        client.login(self.config.gmail_email, self.config.gmail_app_password)
        logger.info("connected to %s as %s", self.config.imap_host, self.config.gmail_email)
        return client

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        for action in ("close", "logout"):
            try:
                getattr(client, action)()
                break
            except Exception:  # noqa: BLE001 - closing must never raise
                continue

    # ----------------------------------------------------------------- loop
    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                processed = self.run_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill it
                failures += 1
                logger.error("poll cycle failed (%s failures): %s", failures, exc)
                self._disconnect()
                if self._stop.is_set():
                    break
                backoff = min(300.0, 5.0 * (2 ** min(failures, 6)))
                self._sleep(backoff + self._rng.uniform(0, 3))
                continue
            wait = self.config.poll_interval_seconds
            if processed == 0:
                self._sleep(wait)
            else:
                self._sleep(1.0)  # drain a burst quickly, then idle

    def run_once(self) -> int:
        """One poll cycle. Returns the number of messages processed."""
        if self._client is None:
            self._client = self._connect()
        client = self._client
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT failed: {status}")

        uids = self._pending_uids(client)
        if not uids:
            self.db.prune_messages(self.config.message_ttl_seconds)
            return 0

        processed = 0
        for uid in uids[:MAX_FETCH_PER_CYCLE]:
            self._handle_uid(client, uid)
            processed += 1
        self.db.prune_messages(self.config.message_ttl_seconds)
        return processed

    def _pending_uids(self, client: ImapClient) -> list[int]:
        """UIDs to fetch, oldest first."""
        last = self.db.get_meta("last_uid")
        if last is None:
            since = (utcnow() - timedelta(days=self.config.initial_lookback_days)).strftime(
                "%d-%b-%Y"
            )
            criteria: tuple[object, ...] = ("SINCE", since)
            logger.info("initial IMAP sync (SINCE %s)", since)
        else:
            criteria = ("UID", f"{int(last) + 1}:*")
        status, data = client.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
        blob = b" ".join(part for part in (data or []) if isinstance(part, bytes))
        uids = sorted({int(token) for token in blob.split() if token.isdigit()})
        if last is not None:
            uids = [uid for uid in uids if uid > int(last)]
        return uids

    def _handle_uid(self, client: ImapClient, uid: int) -> None:
        status, data = client.uid("FETCH", str(uid), "(RFC822)")
        if status != "OK":
            raise RuntimeError(f"IMAP UID FETCH {uid} failed: {status}")
        raw = b""
        for part in data or []:
            if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                raw = part[1]
                break
        if not raw:
            logger.warning("empty body for UID %s", uid)
            self.db.set_meta("last_uid", str(uid))
            return

        parsed = parse_email(raw)
        alias, recipient = match_alias(parsed, self.config)
        if alias is None:
            # Advanced cursor only: this is what stops the original's
            # "re-fetch every unmatched UNSEEN mail forever" loop.
            logger.debug("UID %s not addressed to an alias", uid)
            self.db.set_meta("last_uid", str(uid))
            return

        otp = self._extractor.extract(parsed.text)
        links = extract_link_urls(parsed.text)
        stored = self.db.add_message(
            alias,
            parsed.subject,
            parsed.text,
            otp=otp,
            links=links,
            rfc_message_id=parsed.rfc_message_id,
            sender=parsed.sender,
            recipient=recipient,
            received_at=parsed.received_at,
        )
        self.db.set_meta("last_uid", str(uid))

        if stored is None:
            return
        if stored.duplicate:
            logger.info("duplicate mail for %s ignored", alias)
            return
        if self.on_message is not None:
            try:
                self.on_message(stored)
            except Exception as exc:  # noqa: BLE001
                logger.error("on_message callback failed: %s", exc)
