"""Repository-level packaging guards for the external-link feature.

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "obsidian" / "root" / "usr" / "local" / "bin" / "xdg-open"
SERVICE = ROOT / "obsidian" / "root" / "etc" / "services.d" / "open-url-api" / "run"
DOCKERFILE = ROOT / "obsidian" / "Dockerfile"
MODULE_DIR = ROOT / "obsidian" / "root" / "opt" / "ha-open-url"
sys.path.insert(0, str(MODULE_DIR))

import open_url_server as ts  # noqa: E402


def _is_executable(path: Path) -> bool:
    return bool(os.stat(path).st_mode & 0o111)


def test_xdg_open_launcher_is_executable() -> None:
    # Given the image's PATH overlay
    # When its mode is inspected
    # Then it is executable
    assert _is_executable(LAUNCHER)


def test_open_url_service_is_executable() -> None:
    # Given the S6 longrun service
    # When its mode is inspected
    # Then it is executable
    assert _is_executable(SERVICE)


def test_service_runs_the_daemon_as_abc() -> None:
    # Given the S6 service
    text = SERVICE.read_text(encoding="utf-8")
    # Then it drops to abc and starts the daemon
    assert "s6-setuidgid abc" in text
    assert "open_url_server.py" in text


def test_launcher_resolves_the_forwarder() -> None:
    # Given the launcher
    text = LAUNCHER.read_text(encoding="utf-8")
    # Then it resolves the module relative to itself
    assert "../../../opt/ha-open-url/forwarder.py" in text


def test_dockerfile_adds_the_open_url_location() -> None:
    # Given the image build
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Then the nginx route and its bounds are present
    for fragment in (
        "location /api/open-url",
        "limit_except GET",
        "proxy_read_timeout 70s",
        "proxy_buffering off",
        "proxy_pass http://127.0.0.1:8091",
    ):
        assert fragment in text


def test_dockerfile_adds_the_script_location() -> None:
    # Given the image build
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Then the client script is served
    assert "location /open-url.js" in text
    assert "alias /opt/ha-open-url/open-url.js" in text


def test_dockerfile_injects_the_script_with_a_guard() -> None:
    # Given the image build
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Then the injection is idempotent and targets the regeneration source
    assert text.count("grep -q open-url.js") >= 2
    assert '<script src="./open-url.js"></script>' in text


def test_dockerfile_guards_the_nginx_anchor() -> None:
    # Given the image build
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Then a moved nginx anchor fails the build
    assert "grep -q api/open-url /defaults/default.conf" in text


def test_read_timeout_exceeds_max_wait() -> None:
    # Given the nginx timeout and the daemon's maximum wait
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"proxy_read_timeout (\d+)s", text)
    assert match is not None
    timeout = int(match.group(1))
    # Then the proxy outlives a full hanging GET
    assert ts.MAX_WAIT_SECONDS == 60
    assert ts.MAX_WAIT_SECONDS < timeout
