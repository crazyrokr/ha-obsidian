"""Tests for the external-link daemon (open_url_server.py).

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "obsidian" / "root" / "opt" / "ha-open-url"))

import open_url_server as ts  # noqa: E402


# --------------------------------------------------------------------------
# validate_url
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/a?b=1",
        "http://example.com",
        "HTTPS://EXAMPLE.COM/path",
        "https://example.com/" + "a" * (ts.MAX_URL_LENGTH - len("https://example.com/")),
    ],
)
def test_valid_urls_are_returned_unchanged(value: str) -> None:
    # Given a value with an allowed scheme and a non-empty host
    # When validated
    result = ts.validate_url(value)
    # Then the original value is returned unchanged
    assert result == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "https://",
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "mailto:a@b.c",
        "ftp://example.com",
        " https://example.com",
        "https://exam ple.com",
        "https://example.com/\x00",
        "https://example.com/" + "a" * (ts.MAX_URL_LENGTH - len("https://example.com/") + 1),
        "http://[::1",
    ],
)
def test_invalid_values_are_rejected(value: object) -> None:
    # Given a value outside the scheme allowlist or malformed
    # When validated
    with pytest.raises(ValueError):
        # Then it is rejected
        ts.validate_url(value)


# --------------------------------------------------------------------------
# PendingUrls
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_enqueue_returns_increasing_ids() -> None:
    # Given an empty queue
    pending = ts.PendingUrls()
    # When two URLs are enqueued
    first = pending.enqueue("https://one.example/")
    second = pending.enqueue("https://two.example/")
    # Then the ids increase
    assert first == 1
    assert second == 2


def test_take_returns_fifo() -> None:
    # Given two queued URLs
    pending = ts.PendingUrls()
    first = pending.enqueue("https://one.example/")
    second = pending.enqueue("https://two.example/")
    # When taken
    # Then the oldest is returned first
    assert pending.take(0) == (first, "https://one.example/")
    assert pending.take(0) == (second, "https://two.example/")


def test_take_consumes_an_item() -> None:
    # Given one queued URL
    pending = ts.PendingUrls()
    pending.enqueue("https://one.example/")
    # When it is taken once
    pending.take(0)
    # Then a second take finds nothing
    assert pending.take(0) is None


def test_take_with_zero_wait_returns_none_when_empty() -> None:
    # Given an empty queue
    pending = ts.PendingUrls()
    # When taken with a zero wait
    result = pending.take(0)
    # Then it returns immediately with nothing
    assert result is None


def test_stale_item_is_not_returned() -> None:
    # Given an item older than the TTL
    clock = FakeClock()
    pending = ts.PendingUrls(clock=clock)
    pending.enqueue("https://one.example/")
    clock.advance(30.1)
    # When taken
    result = pending.take(0)
    # Then the stale item is dropped
    assert result is None
    assert len(pending) == 0


def test_item_at_ttl_boundary_is_returned() -> None:
    # Given an item exactly at the TTL boundary
    clock = FakeClock()
    pending = ts.PendingUrls(clock=clock)
    first = pending.enqueue("https://one.example/")
    clock.advance(30.0)
    # When taken
    # Then it is still returned
    assert pending.take(0) == (first, "https://one.example/")


def test_dedupe_within_window_returns_existing_id() -> None:
    # Given the same URL twice within the dedupe window
    clock = FakeClock()
    pending = ts.PendingUrls(clock=clock)
    first = pending.enqueue("https://one.example/")
    clock.advance(1.0)
    # When it is enqueued again
    second = pending.enqueue("https://one.example/")
    # Then the existing id is returned and only one entry remains
    assert second == first
    assert len(pending) == 1


def test_dedupe_outside_window_creates_new_id() -> None:
    # Given the same URL twice outside the dedupe window
    clock = FakeClock()
    pending = ts.PendingUrls(clock=clock)
    first = pending.enqueue("https://one.example/")
    clock.advance(3.0)
    # When it is enqueued again
    second = pending.enqueue("https://one.example/")
    # Then a new id is created and both entries remain
    assert second != first
    assert len(pending) == 2


def test_max_items_drops_oldest() -> None:
    # Given a queue filled to its limit and then one more
    pending = ts.PendingUrls(max_items=8)
    first = pending.enqueue("https://one.example/")
    for index in range(2, 10):
        pending.enqueue(f"https://{index}.example/")
    # When inspected
    # Then the first entry was dropped and the rest remain
    assert len(pending) == 8
    assert pending.take(0) == (first + 1, "https://2.example/")


def test_take_wakes_on_enqueue() -> None:
    # Given a waiter blocked on an empty queue
    pending = ts.PendingUrls()
    result: list[tuple[int, str] | None] = []
    thread = threading.Thread(target=lambda: result.append(pending.take(1.0)))
    thread.start()
    # When a URL is enqueued
    pending.enqueue("https://one.example/")
    thread.join(timeout=2.0)
    # Then the waiter receives it
    assert result == [(1, "https://one.example/")]


def test_two_waiters_do_not_receive_the_same_item() -> None:
    # Given two waiters on an empty queue
    pending = ts.PendingUrls()
    results: list[tuple[int, str] | None] = []
    barrier = threading.Barrier(3)

    def wait() -> None:
        barrier.wait()
        results.append(pending.take(0.5))

    first = threading.Thread(target=wait)
    second = threading.Thread(target=wait)
    first.start()
    second.start()
    # When one URL is enqueued
    barrier.wait()
    pending.enqueue("https://one.example/")
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    # Then exactly one waiter received it
    assert len(results) == 2
    assert sum(result is not None for result in results) == 1


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@pytest.fixture
def server():
    server = ts.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=lambda: server.serve_forever(0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=5
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _post_json(server, path, payload):
    return _request(
        server,
        "POST",
        path,
        body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 60.0),
        ("30", 30.0),
        ("0", 1.0),
        ("-5", 1.0),
        ("999", 60.0),
        ("abc", 60.0),
        ("nan", 60.0),
        ("inf", 60.0),
    ],
)
def test_clamp_wait(value: str | None, expected: float) -> None:
    # Given a raw wait value
    # When clamped
    result = ts.clamp_wait(value)
    # Then the default or the [1, 60] bound is returned
    assert result == expected


def test_limiter_caps_concurrent_waiters() -> None:
    # Given a limiter with four slots
    limiter = ts.WaiterLimiter(4)
    # When four slots are taken
    assert [limiter.acquire() for _ in range(4)] == [True, True, True, True]
    # Then the fifth is refused
    assert limiter.acquire() is False
    # And releasing one allows the next
    limiter.release()
    assert limiter.acquire() is True
    assert limiter.active == 4


def test_post_queues_url(server) -> None:
    # Given a running daemon
    # When a valid URL is posted
    status, headers, body = _post_json(
        server, "/api/open-url", {"url": "https://example.com/a"}
    )
    payload = json.loads(body)
    # Then it is queued and the response is 202
    assert status == 202
    assert payload["status"] == "queued"
    assert headers["Cache-Control"] == "no-store"
    # And the queue holds it
    assert server.pending.take(0) == (payload["id"], "https://example.com/a")


def test_post_rejects_invalid_scheme(server) -> None:
    # Given a running daemon
    # When a disallowed-scheme URL is posted
    status, _, body = _post_json(
        server, "/api/open-url", {"url": "javascript:alert(1)"}
    )
    # Then it is rejected and nothing is queued
    assert status == 400
    assert json.loads(body)["status"] == "error"
    assert len(server.pending) == 0


def test_post_rejects_malformed_json(server) -> None:
    # Given a running daemon
    # When the body is not JSON
    status, _, _ = _request(
        server,
        "POST",
        "/api/open-url",
        body="{",
        headers={"Content-Type": "application/json"},
    )
    # Then the request is rejected
    assert status == 400


def test_post_rejects_missing_url(server) -> None:
    # Given a running daemon
    # When the JSON object has no url
    status, _, _ = _post_json(server, "/api/open-url", {"other": 1})
    # Then the request is rejected
    assert status == 400


def test_post_rejects_oversized_body(server) -> None:
    # Given a running daemon
    # When the body exceeds the limit
    status, _, _ = _post_json(
        server,
        "/api/open-url",
        {"url": "https://example.com/" + "a" * ts.MAX_BODY_BYTES},
    )
    # Then the request is rejected
    assert status == 400


def test_post_unknown_path_is_404(server) -> None:
    # Given a running daemon
    # When a URL is posted to another path
    status, _, _ = _post_json(server, "/api/other", {"url": "https://example.com"})
    # Then it is not found
    assert status == 404


def test_get_returns_a_queued_url(server) -> None:
    # Given a queued URL
    _, _, body = _post_json(server, "/api/open-url", {"url": "https://example.com/a"})
    item_id = json.loads(body)["id"]
    # When a client asks for the next URL
    status, headers, body = _request(server, "GET", "/api/open-url?wait=1")
    # Then it is returned with no-store
    assert status == 200
    assert json.loads(body) == {"url": "https://example.com/a", "id": item_id}
    assert headers["Cache-Control"] == "no-store"


def test_get_times_out_with_204(server) -> None:
    # Given an empty queue
    # When a client waits for a URL
    status, headers, body = _request(server, "GET", "/api/open-url?wait=1")
    # Then the wait ends with an empty response
    assert status == 204
    assert body == b""
    assert "Content-Type" not in headers


def test_get_returns_429_when_all_waiter_slots_are_taken(server) -> None:
    # Given every waiter slot is taken
    for _ in range(ts.MAX_WAITERS):
        assert server.limiter.acquire()
    try:
        # When a client polls
        status, headers, _ = _request(server, "GET", "/api/open-url?wait=1")
        # Then it is refused with Retry-After
        assert status == 429
        assert headers["Retry-After"] == "2"
    finally:
        for _ in range(ts.MAX_WAITERS):
            server.limiter.release()


def test_get_unknown_path_is_404(server) -> None:
    # Given a running daemon
    # When a client polls another path
    status, _, _ = _request(server, "GET", "/api/other?wait=1")
    # Then it is not found
    assert status == 404


def test_enqueue_wakes_a_blocked_get(server) -> None:
    # Given a client polling with a two-second wait
    responses: list[tuple[int, bytes]] = []

    def poll() -> None:
        status, _, body = _request(server, "GET", "/api/open-url?wait=2")
        responses.append((status, body))

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.1)
    # When a URL is posted
    _post_json(server, "/api/open-url", {"url": "https://example.com/wake"})
    thread.join(timeout=3.0)
    # Then the waiting client receives it
    assert responses and responses[0][0] == 200
    assert json.loads(responses[0][1])["url"] == "https://example.com/wake"
