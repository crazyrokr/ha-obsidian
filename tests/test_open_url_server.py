"""Tests for the external-link daemon (open_url_server.py).

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import sys
import threading
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
