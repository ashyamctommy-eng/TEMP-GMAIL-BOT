"""Callback-data encoding.

Telegram caps ``callback_data`` at 64 bytes and rejects the whole message when
it is exceeded. Building strings by concatenation with ``_`` separators (as the
original did) also made aliases containing ``_`` ambiguous -- and the generator
produced ``swift_tiger_12`` style names.

Every builder here is total and length-checked by ``tests/test_callbacks.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_CALLBACK_BYTES = 64
SEP = ":"

# Non-parameterised actions
GEN = "a:gen"
OTP_LIST = "a:otp:0"
HISTORY = "a:hist"
FEEDBACK = "a:fb"
HELP = "a:help"
START = "a:start"

# Force-join
JOIN_VERIFY = "jv"

# Admin panel. Everything under this prefix is re-checked against ADMIN_USER_ID
# in the callback handler, so a forwarded/stale button cannot be replayed by a
# non-admin.
ADMIN_PREFIX = "ad"
ADMIN_PANEL = "ad:panel"
ADMIN_STATS = "ad:stats"
ADMIN_BROADCAST = "ad:bc"
ADMIN_BAN = "ad:ban"
ADMIN_UNBAN = "ad:unban"
ADMIN_CHANNELS = "ad:chs"
ADMIN_ADD_CHANNEL = "ad:addch"
ADMIN_DEL_CHANNEL = "ad:delch"
ADMIN_TRACKERS = "ad:trk"
ADMIN_CLOSE = "ad:close"


def _join(*parts: str) -> str:
    data = SEP.join(str(part) for part in parts)
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data too long ({len(data.encode())} bytes): {data!r}")
    return data


def view_alias(alias: str, page: int = 0) -> str:
    return _join("v", alias.lower(), page)


def delete_alias(alias: str) -> str:
    return _join("d", alias.lower())


def restore_alias(alias: str) -> str:
    return _join("r", alias.lower())


def confirm_create(alias: str) -> str:
    return _join("c", alias.lower())


def otp_page(page: int) -> str:
    return _join("a", "otp", page)


def reveal_secret(message_id: int, field: str) -> str:
    return _join("s", message_id, field)


def parse(data: str) -> tuple[str, list[str]]:
    """Split callback data into ``(action, params)``.

    Unknown or malformed data returns ``("", [])`` instead of raising: callback
    data comes from the client and cannot be trusted.
    """
    if not data or len(data.encode()) > MAX_CALLBACK_BYTES:
        return "", []
    action, _, rest = data.partition(SEP)
    params = rest.split(SEP) if rest else []
    return action, params


def remove_channel(chat_id: str) -> str:
    return _join("ad", "rmch", chat_id)


def parse_remove_channel(data: str) -> str | None:
    action, params = parse(data)
    if action != "ad" or param0(params) != "rmch" or len(params) < 2:
        return None
    return params[1]


def param0(params: list[str]) -> str:
    return params[0] if params else ""


def remove_tracker(index: int) -> str:
    """Index, not the pattern itself: host globs can be long, 64 bytes is a cap."""
    return _join("ad", "rmtrk", index)


def parse_remove_tracker(data: str) -> int | None:
    action, params = parse(data)
    if action != "ad" or param0(params) != "rmtrk" or len(params) < 2:
        return None
    return int(params[1]) if params[1].isdigit() else None


def parse_view(data: str) -> tuple[str | None, int]:
    action, params = parse(data)
    if action != "v" or not params:
        return None, 0
    alias = params[0]
    page = 0
    if len(params) > 1 and params[1].isdigit():
        page = int(params[1])
    return alias, page
