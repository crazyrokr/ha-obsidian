"""URL validation for the external-link daemon.

The pending-URL queue and the loopback HTTP server are added in later tasks.
"""

from __future__ import annotations

from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 8192


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
