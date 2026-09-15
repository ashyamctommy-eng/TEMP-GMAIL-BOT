"""Test doubles: no network, no Telegram token, no Gmail account."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeUser:
    def __init__(self, user_id: int = 1, first_name: str = "Test", last_name: str = "", username: str = "tester"):
        self.id = user_id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


@dataclass
class Sent:
    text: str
    markup: Any = None
    parse_mode: str | None = None


class FakeMessage:
    def __init__(self, *, text: str = "", caption: str | None = None, photo: list | None = None):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.sent: list[Sent] = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.sent.append(Sent(text, reply_markup, parse_mode))
        return self

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.sent.append(Sent(text, reply_markup, parse_mode))
        return self

    @property
    def last(self) -> Sent:
        return self.sent[-1]


class FakePhoto:
    def __init__(self, file_id: str = "photo-1", file_size: int = 1024):
        self.file_id = file_id
        self.file_size = file_size


class FakeQuery:
    def __init__(self, *, data: str, user: FakeUser, message: FakeMessage):
        self.data = data
        self.from_user = user
        self.message = message
        self.answers: list[str | None] = []
        self.edits: list[Sent] = []

    async def answer(self, text=None, show_alert=False):  # noqa: D401
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.edits.append(Sent(text, reply_markup, parse_mode))
        return self.message


class FakeUpdate:
    def __init__(self, *, user: FakeUser, message: FakeMessage | None = None, query: FakeQuery | None = None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = query


class FakeChatMember:
    def __init__(self, status: str = "administrator", can_send_messages: bool = True):
        self.status = status
        self.can_send_messages = can_send_messages


class FakeBot:
    """Records outbound calls; ``fail`` can inject errors per chat id."""

    def __init__(self, member: FakeChatMember | None = None):
        self.id = 424242
        self.member = member or FakeChatMember()
        self.messages: list[tuple[int, str, Any]] = []
        self.photos: list[tuple[int, str, str]] = []
        self.deleted: list[int] = []
        self.commands: list[Any] = []
        self.fail_for: set[int] = set()
        self.get_chat_member_calls = 0

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kwargs):
        self.get_chat_member_calls += 0
        if chat_id in self.fail_for:
            from telegram.error import TelegramError

            raise TelegramError("injected failure")
        self.messages.append((chat_id, text, parse_mode))
        return FakeMessage(text=text)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kwargs):
        if chat_id in self.fail_for:
            from telegram.error import TelegramError

            raise TelegramError("injected failure")
        self.photos.append((chat_id, photo, caption or ""))
        return FakeMessage(text=caption or "")

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def get_chat_member(self, chat_id, user_id):
        self.get_chat_member_calls += 1
        return self.member

    async def set_my_commands(self, commands):
        self.commands = list(commands)


class FakeContext:
    def __init__(self, *, bot: FakeBot | None = None, args: list[str] | None = None, user_data: dict | None = None):
        self.bot = bot or FakeBot()
        self.args = args or []
        self.user_data = user_data if user_data is not None else {}
        self.error: BaseException | None = None
        self.bot_data: dict = {}


# --------------------------------------------------------------------- IMAP
@dataclass
class FakeMailbox:
    """Minimal IMAP server double."""

    messages: dict[int, bytes] = field(default_factory=dict)
    events: list[tuple] = field(default_factory=list)

    def add(self, uid: int, raw: bytes) -> None:
        self.messages[uid] = raw


class FakeImap:
    def __init__(self, mailbox: FakeMailbox, *, fail_connect: bool = False):
        self.mailbox = mailbox
        self.fail_connect = fail_connect
        self.selected: list[tuple[str, bool]] = []
        self.stored_flags: list[tuple] = []
        self.searches: list[tuple] = []
        self.fetches: list[int] = []
        self.logged_out = False
        self.closed = False

    # -- connection lifecycle
    def login(self, user, password):
        if self.fail_connect:
            raise RuntimeError("login failed")
        self.mailbox.events.append(("login", user))
        return "OK", [b"logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        self.selected.append((mailbox, readonly))
        return "OK", [b"1"]

    def close(self):
        self.closed = True
        return "OK", [b""]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    # -- commands
    def uid(self, command, *args):
        self.mailbox.events.append((command, args))
        if command == "SEARCH":
            self.searches.append(args)
            criteria = [str(a) for a in args if a is not None]
            uids = sorted(self.mailbox.messages)
            if any(a.upper() == "UID" for a in criteria):
                spec = next(a for a in criteria if ":" in a)
                low = int(spec.split(":", 1)[0])
                uids = [uid for uid in uids if uid >= low]
            elif any(a.upper() == "SINCE" for a in criteria):
                uids = list(uids)  # the double has no dates; return everything
            return "OK", [b" ".join(str(uid).encode() for uid in uids)]
        if command == "FETCH":
            uid = int(args[0])
            self.fetches.append(uid)
            raw = self.mailbox.messages.get(uid)
            if raw is None:
                return "NO", [b""]
            header = f"{{1}} (RFC822 {{{len(raw)}}}".encode()
            return "OK", [(header, raw), b")"]
        if command == "STORE":
            self.stored_flags.append(args)
            return "OK", [b""]
        return "NO", [b"unsupported"]


def make_raw_email(
    *,
    to: str,
    subject: str = "Hello",
    body: str = "Body text",
    html: str | None = None,
    message_id: str = "<abc@example.com>",
    sender: str = "Sender <sender@example.com>",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    parts = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        "Date: Mon, 15 Sep 2026 11:00:00 +0000",
        "MIME-Version: 1.0",
    ]
    for key, value in (extra_headers or {}).items():
        parts.append(f"{key}: {value}")

    if html is None:
        parts += ["Content-Type: text/plain; charset=utf-8", "", body]
        return "\r\n".join(parts).encode()

    boundary = "BOUND"
    parts += [
        f'Content-Type: multipart/alternative; boundary="{boundary}"',
        "",
        f"--{boundary}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
        f"--{boundary}",
        "Content-Type: text/html; charset=utf-8",
        "",
        html,
        f"--{boundary}--",
        "",
    ]
    return "\r\n".join(parts).encode()
