"""Forward http(s) xdg-open calls to the loopback link daemon.

The forwarder runs behind the /usr/local/bin/xdg-open launcher. It forwards a
single http(s) URL and falls back to the real xdg-open for every other
argument or when the daemon cannot be reached.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import NoReturn
from urllib.parse import urlsplit

from open_url_server import validate_url

DEFAULT_ENDPOINT = "http://127.0.0.1:8091/api/open-url"
DEFAULT_REAL_XDG_OPEN = "/usr/bin/xdg-open"
DEFAULT_TIMEOUT = 2.0


def should_forward(argv: list[str]) -> bool:
    """True when argv is exactly one URL the daemon accepts."""
    if len(argv) != 1:
        return False
    try:
        validate_url(argv[0])
    except ValueError:
        return False
    return True


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
    real = os.environ.get("XDG_OPEN_REAL", DEFAULT_REAL_XDG_OPEN)
    endpoint = os.environ.get("OPEN_URL_ENDPOINT", DEFAULT_ENDPOINT)
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
