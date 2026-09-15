"""Typed return values.

The original code returned raw tuples and every caller unpacked them positionally
(``msg[1]``, ``msg[7]`` ...). One schema change broke every call site silently.
These dataclasses replace that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601 (``2026-09-15T11:44:39.123Z``).

    Fixed width matters: the DB sorts these as text, so lexicographic order must
    equal chronological order.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_iso(value: str) -> datetime:
    """Parse either our ISO format or a legacy ``YYYY-MM-DD HH:MM:SS`` value."""
    text = (value or "").strip()
    if not text:
        return utcnow()
    try:
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return utcnow()


@dataclass(frozen=True)
class Alias:
    name: str
    active: bool
    created_at: datetime
    format: str | None = None

    @property
    def created_display(self) -> str:
        return self.created_at.strftime("%d %b %H:%M UTC")


@dataclass(frozen=True)
class Message:
    id: int
    alias: str
    subject: str
    body: str
    received_at: datetime
    seen: bool
    otp: str | None = None
    links: list[str] = field(default_factory=list)
    sender: str | None = None

    @property
    def received_display(self) -> str:
        return self.received_at.strftime("%H:%M UTC")

    @property
    def has_secret(self) -> bool:
        return bool(self.otp or self.links)


@dataclass(frozen=True)
class StoredMessage:
    """What the mail poller gets back when a message is accepted."""

    user_id: int
    message_id: int
    alias: str
    subject: str
    otp: str | None
    links: list[str]
    received_at: datetime
    preview: str = ""
    duplicate: bool = False


@dataclass(frozen=True)
class AliasAddResult:
    ok: bool
    reason: str = ""

    #: Machine-readable reasons so the UI can give an actual next step.
    CREATED = "created"
    TAKEN = "taken"
    YOURS_ALREADY = "yours_already"
    INVALID = "invalid"
    LIMIT_REACHED = "limit_reached"
