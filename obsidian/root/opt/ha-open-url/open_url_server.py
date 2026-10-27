"""URL validation and the pending-link queue for the external-link daemon.

The loopback HTTP server is added in a later task.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 8192
DEFAULT_MAX_ITEMS = 8
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_DEDUPE_WINDOW_SECONDS = 2.0


def validate_url(value: object) -> str:
    """Return the original URL, or raise ValueError when it is not allowed.

    Only absolute http(s) URLs with a host are accepted. Whitespace and
    control characters are rejected so a link cannot smuggle a second
    argument or a log-breaking line into the diagnostic path.
    """
    if not isinstance(value, str):
        raise ValueError(f"URL must be a string, got {type(value).__name__}")
    if not 0 < len(value) <= MAX_URL_LENGTH:
        raise ValueError(f"URL length must be 1..{MAX_URL_LENGTH}")
    for character in value:
        if character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F:
            raise ValueError("URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"malformed URL: {error}") from None
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("URL must include a host")
    return value


class PendingUrls:
    """A bounded, thread-safe queue of links waiting for a browser client.

    The queue holds at most `max_items` URLs, drops items older than `ttl`,
    and collapses repeats of the same URL within `dedupe_window`. `take`
    blocks on a condition variable, so an idle client costs no CPU. The
    injectable clock drives the TTL and dedupe decisions; the blocking wait
    always uses the monotonic clock.
    """

    def __init__(
        self,
        max_items: int = DEFAULT_MAX_ITEMS,
        ttl: float = DEFAULT_TTL_SECONDS,
        dedupe_window: float = DEFAULT_DEDUPE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._items: deque[tuple[int, str, float]] = deque(maxlen=max_items)
        self._ttl = ttl
        self._dedupe_window = dedupe_window
        self._clock = clock
        self._ids = itertools.count(1)
        self._condition = threading.Condition()

    def __len__(self) -> int:
        with self._condition:
            self._drop_stale_locked(self._clock())
            return len(self._items)

    def _drop_stale_locked(self, now: float) -> None:
        while self._items and now - self._items[0][2] > self._ttl:
            self._items.popleft()

    def enqueue(self, url: str) -> int:
        with self._condition:
            now = self._clock()
            self._drop_stale_locked(now)
            for item_id, item_url, enqueued_at in reversed(self._items):
                if item_url == url and now - enqueued_at <= self._dedupe_window:
                    return item_id
            item_id = next(self._ids)
            self._items.append((item_id, url, now))
            self._condition.notify_all()
            return item_id

    def take(self, wait_seconds: float) -> tuple[int, str] | None:
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                now = self._clock()
                self._drop_stale_locked(now)
                if self._items:
                    item_id, url, _ = self._items.popleft()
                    return item_id, url
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
