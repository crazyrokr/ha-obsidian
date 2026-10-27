"""Tests for the external-link daemon (open_url_server.py).

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import sys
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
