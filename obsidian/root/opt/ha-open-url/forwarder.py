"""Forward link-opening calls to the loopback daemon.

The forwarder runs behind the /usr/local/bin/xdg-open and xdg-email
launchers. It forwards a single allowed link (http(s), mailto, tel or sms)
to the viewing browser, hands a single Obsidian deep link to the
in-container app in the default mode, and falls back for every other
argument or when the chosen target cannot be reached. The email mode
(selected by the xdg-email launcher) falls back to the real xdg-email
instead of xdg-open.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import NoReturn
from urllib.parse import urlsplit

from open_url_server import MAX_URL_LENGTH, validate_url

DEFAULT_ENDPOINT = "http://127.0.0.1:8091/api/open-url"
DEFAULT_OBSIDIAN_LAUNCHER = "/usr/bin/obsidian"
DEFAULT_REAL_XDG_EMAIL = "/usr/bin/xdg-email"
DEFAULT_REAL_XDG_OPEN = "/usr/bin/xdg-open"
DEFAULT_TIMEOUT = 2.0
EMAIL_MODE = "email"
OBSIDIAN_PREFIX = "obsidian://"


def should_forward(argv: list[str]) -> bool:
    """True when argv is exactly one link the daemon accepts."""
    if len(argv) != 1:
        return False
    try:
        validate_url(argv[0])
    except ValueError:
        return False
    return True


def should_open_in_obsidian(argv: list[str]) -> bool:
    """True when argv is exactly one obsidian:// deep link the app accepts.

    The prefix check is case-sensitive because Obsidian's own handler is:
    an upper-case scheme would be ignored by the running instance.
    """
    if len(argv) != 1:
        return False
    value = argv[0]
    if not value.startswith(OBSIDIAN_PREFIX):
        return False
    if not 0 < len(value) <= MAX_URL_LENGTH:
        return False
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.netloc)


def send(
    url: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """POST the URL as JSON; raise OSError on a transport or status error."""
    payload = json.dumps({"url": url}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise OSError(f"link daemon returned {response.status}")


def exec_app(argv: list[str], launcher: str = DEFAULT_OBSIDIAN_LAUNCHER) -> NoReturn:
    """Replace this process with the Obsidian launcher."""
    os.execv(launcher, [launcher, *argv])


def exec_real(argv: list[str], real: str = DEFAULT_REAL_XDG_OPEN) -> NoReturn:
    """Replace this process with the real xdg-open."""
    os.execv(real, [real, *argv])


def _diagnose(argv: list[str], outcome: str, error: Exception | None = None) -> None:
    url = argv[0] if len(argv) == 1 else ""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme or "none"
        host = parsed.hostname or "-"
    except ValueError:
        scheme = "none"
        host = "-"
    line = f"[ha-open-url] outcome={outcome} scheme={scheme} host={host}"
    if error is not None:
        line += f" error={type(error).__name__}"
    if os.environ.get("OPEN_URL_DEBUG") == "1" and url:
        line += f" url={url}"
    print(line, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = os.environ.get("OPEN_URL_MODE", "")
    if mode == EMAIL_MODE:
        real = os.environ.get("XDG_EMAIL_REAL", DEFAULT_REAL_XDG_EMAIL)
    else:
        real = os.environ.get("XDG_OPEN_REAL", DEFAULT_REAL_XDG_OPEN)
    endpoint = os.environ.get("OPEN_URL_ENDPOINT", DEFAULT_ENDPOINT)
    launcher = os.environ.get("OBSIDIAN_LAUNCHER", DEFAULT_OBSIDIAN_LAUNCHER)
    if mode != EMAIL_MODE and should_open_in_obsidian(args):
        _diagnose(args, "app")
        try:
            exec_app(args, launcher)
        except OSError as error:
            _diagnose(args, "fallback", error)
            exec_real(args, real)
    if not should_forward(args):
        _diagnose(args, "fallback")
        exec_real(args, real)
    try:
        send(args[0], endpoint)
    except Exception as error:  # noqa: BLE001 - fallback is the contract
        _diagnose(args, "fallback", error)
        exec_real(args, real)
    _diagnose(args, "forwarded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
