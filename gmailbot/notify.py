"""Thread-safe hand-off from the IMAP thread to the asyncio event loop.

The original pushed into a ``queue.Queue`` and a coroutine polled it **every
second** forever, with no retry, no back-pressure and no handling of Telegram's
``RetryAfter``. Here:

* producers (any thread) call :meth:`Notifier.emit`, which marshals onto the
  loop with ``call_soon_threadsafe`` -- no polling,
* items emitted before the bot is ready are buffered instead of dropped,
* sends are retried with backoff, ``RetryAfter`` (flood control) is honoured,
  and blocked users are dropped quietly,
* the queue is bounded, so a flood cannot exhaust memory.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .models import StoredMessage

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
QUEUE_SIZE = 500


@dataclass
class Notification:
    user_id: int
    text: str
    markup: InlineKeyboardMarkup | None = None
    kind: str = "message"
    attempts: int = field(default=0)


class Notifier:
    def __init__(
        self,
        render: Callable[[StoredMessage], Notification],
        *,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._render = render
        self._max_attempts = max_attempts
        self._loop: asyncio.AbstractEventLoop | None = None
        self._application = None
        self._queue: asyncio.Queue[Notification] | None = None
        self._buffered: deque[Notification] = deque(maxlen=QUEUE_SIZE)
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._blocked: set[int] = set()

    # ------------------------------------------------------------ lifecycle
    def bind(self, loop: asyncio.AbstractEventLoop, application) -> None:
        with self._lock:
            self._loop = loop
            self._application = application
            self._queue = asyncio.Queue(QUEUE_SIZE)
            buffered, self._buffered = list(self._buffered), deque(maxlen=QUEUE_SIZE)
        for item in buffered:
            self._queue.put_nowait(item)
        if buffered:
            logger.info("flushed %d notification(s) queued before startup", len(buffered))

    def start(self) -> None:
        if self._queue is None:
            raise RuntimeError("Notifier.bind() must be called before start()")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="notifier")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------ producers
    def emit(self, stored: StoredMessage) -> None:
        """Called from the poller thread. Never blocks, never raises."""
        try:
            notification = self._render(stored)
        except Exception as exc:  # noqa: BLE001 - a bad template must not kill mail
            logger.error("could not render notification: %s", exc)
            return
        with self._lock:
            loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            logger.debug("notifier not bound yet; buffering notification")
            with self._lock:
                self._buffered.append(notification)
            return
        loop.call_soon_threadsafe(self._enqueue, notification)

    def _enqueue(self, notification: Notification) -> None:
        queue = self._queue
        if queue is None:
            return
        if queue.full():
            dropped = queue.get_nowait()
            logger.warning("notification queue full; dropped one for user %s", dropped.user_id)
        queue.put_nowait(notification)

    # -------------------------------------------------------------- consumer
    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            notification = await self._queue.get()
            if notification.user_id in self._blocked:
                continue
            await self._deliver(notification)

    async def _deliver(self, notification: Notification) -> None:
        application = self._application
        if application is None:  # pragma: no cover - defensive
            return
        while notification.attempts < self._max_attempts:
            notification.attempts += 1
            try:
                await application.bot.send_message(
                    chat_id=notification.user_id,
                    text=notification.text,
                    reply_markup=notification.markup,
                    parse_mode="HTML",
                )
                return
            except RetryAfter as exc:
                delay = float(getattr(exc, "retry_after", 1)) + 0.5
                logger.warning("flood control; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
            except Forbidden:
                # User blocked the bot or deleted the chat: stop trying forever.
                self._blocked.add(notification.user_id)
                logger.info("user %s blocked the bot; notifications disabled", notification.user_id)
                return
            except BadRequest as exc:
                logger.error("bad notification for %s: %s", notification.user_id, exc)
                return
            except TelegramError as exc:
                logger.warning(
                    "send failed (attempt %d/%d) for %s: %s",
                    notification.attempts,
                    self._max_attempts,
                    notification.user_id,
                    exc,
                )
                await asyncio.sleep(1.5 * notification.attempts)
        logger.error("giving up on notification for user %s", notification.user_id)


class RateLimiter:
    """Sliding-window limiter, in-process.

    The original had no limits at all: ``/generate`` could be spammed to grow the
    alias table without bound, and ``/broadcast`` sent in a tight loop with no
    delay, which trips Telegram flood control on any real user base.
    """

    def __init__(self, max_calls: int, per_seconds: float, *, clock=time.monotonic) -> None:
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and now - window[0] > self.per_seconds:
                window.popleft()
            if len(window) >= self.max_calls:
                return False, round(self.per_seconds - (now - window[0]), 1)
            window.append(now)
            if len(self._hits) > 5000:  # keep the dict from growing forever
                self._hits = {
                    key_: hits
                    for key_, hits in self._hits.items()
                    if hits and now - hits[-1] <= self.per_seconds
                }
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
