"""URL validation, the pending-link queue, and the loopback HTTP API.

The daemon accepts links from the xdg-open forwarder on loopback and serves
them to the dashboard client through a hanging GET.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import select
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse, urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 8192
DEFAULT_MAX_ITEMS = 8
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_DEDUPE_WINDOW_SECONDS = 2.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091
DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 60.0
MAX_WAITERS = 4
MAX_BODY_BYTES = 16384


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
    blocks on a condition variable in short slices and checks an optional
    disconnect predicate before consuming, so an aborted request never
    steals a queued link. The injectable clock drives the TTL and dedupe
    decisions; the blocking wait always uses the monotonic clock.
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

    def take(
        self,
        wait_seconds: float,
        is_disconnected: Callable[[], bool] | None = None,
        slice_seconds: float = 0.5,
    ) -> tuple[int, str] | None:
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                now = self._clock()
                self._drop_stale_locked(now)
                if is_disconnected is not None and is_disconnected():
                    return None
                if self._items:
                    item_id, url, _ = self._items.popleft()
                    return item_id, url
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, slice_seconds))


def clamp_wait(value: str | None) -> float:
    """Return the requested wait in seconds, clamped to the 1..60 range."""
    if value is None:
        return DEFAULT_WAIT_SECONDS
    try:
        requested = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS
    if not math.isfinite(requested):
        return DEFAULT_WAIT_SECONDS
    return max(1.0, min(MAX_WAIT_SECONDS, requested))


def peer_disconnected(connection: socket.socket) -> bool:
    """True when a waiting client has closed its end of the connection."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


class WaiterLimiter:
    """Cap the number of clients holding a hanging GET at once."""

    def __init__(self, limit: int = MAX_WAITERS) -> None:
        self._limit = limit
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1


class OpenUrlRequestHandler(BaseHTTPRequestHandler):
    server_version = "ha-open-url"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        route = urlparse(self.path)
        if route.path.rstrip("/") != "/api/open-url":
            self.close_connection = True
            self._respond_json(404, {"status": "error", "error": "not found"})
            return
        length_header = self.headers.get("Content-Length")
        try:
            content_length = int(length_header) if length_header is not None else 0
        except ValueError:
            self.close_connection = True
            self._respond_json(400, {"status": "error", "error": "invalid Content-Length"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.close_connection = True
            self._respond_json(400, {"status": "error", "error": "invalid body size"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, UnicodeDecodeError):
            self._respond_json(400, {"status": "error", "error": "malformed JSON"})
            return
        if not isinstance(payload, dict):
            self._respond_json(400, {"status": "error", "error": "body must be an object"})
            return
        try:
            url = validate_url(payload.get("url"))
        except ValueError as error:
            self._respond_json(400, {"status": "error", "error": str(error)})
            return
        item_id = self.server.pending.enqueue(url)
        self._respond_json(202, {"status": "queued", "id": item_id})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        route = urlparse(self.path)
        if route.path.rstrip("/") != "/api/open-url":
            self._respond_json(404, {"status": "error", "error": "not found"})
            return
        wait = clamp_wait(parse_qs(route.query).get("wait", [None])[0])
        if not self.server.limiter.acquire():
            self._respond_json(
                429,
                {"status": "error", "error": "too many waiters"},
                {"Retry-After": "2"},
            )
            return
        aborted = False

        def disconnected() -> bool:
            nonlocal aborted
            if peer_disconnected(self.connection):
                aborted = True
                return True
            return False

        try:
            item = self.server.pending.take(wait, disconnected)
        finally:
            self.server.limiter.release()
        if aborted:
            self.close_connection = True
            return
        if item is None:
            self._respond_no_content()
            return
        item_id, url = item
        self._respond_json(200, {"url": url, "id": item_id})

    def _respond_json(
        self, code: int, payload: dict, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _respond_no_content(self) -> None:
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class OpenUrlServer(ThreadingHTTPServer):
    """A threaded loopback server carrying the pending queue and waiter cap."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        pending: PendingUrls | None = None,
    ) -> None:
        super().__init__(server_address, OpenUrlRequestHandler)
        self.pending = pending if pending is not None else PendingUrls()
        self.limiter = WaiterLimiter()


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    pending: PendingUrls | None = None,
) -> OpenUrlServer:
    """Create the loopback server; tests pass port 0 for an ephemeral port."""
    return OpenUrlServer((host, port), pending=pending)


def main() -> None:
    host = os.environ.get("OPEN_URL_HOST", DEFAULT_HOST)
    port = int(os.environ.get("OPEN_URL_PORT", str(DEFAULT_PORT)))
    server = make_server(host, port)
    print(f"[ha-open-url] listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
