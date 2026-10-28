"""Tests for the xdg-open forwarder and its launcher.

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "obsidian" / "root" / "opt" / "ha-open-url"
LAUNCHER = ROOT / "obsidian" / "root" / "usr" / "local" / "bin" / "xdg-open"
sys.path.insert(0, str(MODULE_DIR))

import forwarder  # noqa: E402


class StubServer:
    """A local daemon stand-in that records each POST and returns a status."""

    def __init__(self, status: int = 202) -> None:
        self.records: list[dict] = []
        self._status = status
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._make_handler()
        )
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(0.01), daemon=True
        )
        self._thread.start()

    @property
    def endpoint(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/api/open-url"

    def _make_handler(self):
        records = self.records
        status = self._status

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                records.append(
                    {
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type"),
                        "body": json.loads(body),
                    }
                )
                payload = b'{"status":"queued","id":1}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def _write_real_stub(tmp_path: Path) -> Path:
    marker = tmp_path / "real-xdg-open-args"
    stub = tmp_path / "xdg-open-stub"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{marker}'\nexit 0\n")
    stub.chmod(0o755)
    return stub


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["https://example.com"], True),
        (["http://example.com"], True),
        (["HTTPS://EXAMPLE.COM"], True),
        (["file:///etc/passwd"], False),
        (["javascript:alert(1)"], False),
        (["mailto:a@b.c"], False),
        (["/config/a.pdf"], False),
        ([], False),
        (["https://a", "https://b"], False),
    ],
)
def test_should_forward(argv: list[str], expected: bool) -> None:
    # Given an argument list
    # When deciding whether to forward
    result = forwarder.should_forward(argv)
    # Then only a single allowed http(s) URL is forwarded
    assert result is expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["obsidian://show-plugin?id=obsidian-importer"], True),
        (["obsidian://open?vault=main&file=Note"], True),
        (["OBSIDIAN://show-plugin?id=obsidian-importer"], False),
        (["Obsidian://show-plugin?id=obsidian-importer"], False),
        (["obsidian://"], False),
        (["obsidian:show-plugin?id=obsidian-importer"], False),
        (["obsidian:///show-plugin?id=obsidian-importer"], False),
        (["https://example.com"], False),
        (["file:///etc/passwd"], False),
        (["obsidian://open", "obsidian://open"], False),
        ([], False),
        (["obsidian://open\n"], False),
        (["obsidian://open?" + "a" * 8192], False),
    ],
)
def test_should_open_in_obsidian(argv: list[str], expected: bool) -> None:
    # Given an argument list
    # When deciding whether it is a single Obsidian deep link
    result = forwarder.should_open_in_obsidian(argv)
    # Then only one well-formed obsidian:// URI with an action is accepted
    assert result is expected


def test_main_opens_an_obsidian_uri_in_the_app(monkeypatch) -> None:
    # Given a single Obsidian deep link and stubs for every other path
    calls: list[tuple[list[str], str]] = []

    def fake_app(argv: list[str], launcher: str) -> None:
        calls.append((list(argv), launcher))
        raise SystemExit(0)

    def fail(*args, **kwargs):
        raise AssertionError("must not forward or fall back")

    monkeypatch.setattr(forwarder, "exec_app", fake_app)
    monkeypatch.setattr(forwarder, "exec_real", fail)
    monkeypatch.setattr(forwarder, "send", fail)
    monkeypatch.setenv("OBSIDIAN_LAUNCHER", "/usr/bin/obsidian")
    with pytest.raises(SystemExit):
        # When the forwarder runs
        forwarder.main(["obsidian://show-plugin?id=obsidian-importer"])
    # Then the app launcher receives the URI unchanged
    assert calls == [
        (["obsidian://show-plugin?id=obsidian-importer"], "/usr/bin/obsidian")
    ]


def test_main_falls_back_when_the_obsidian_launcher_fails(monkeypatch) -> None:
    # Given a deep link and a launcher that cannot be executed
    calls: list[tuple[list[str], str]] = []

    def fake_app(argv: list[str], launcher: str) -> None:
        raise OSError("launcher missing")

    def fake_real(argv: list[str], real: str) -> None:
        calls.append((list(argv), real))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_app", fake_app)
    monkeypatch.setattr(forwarder, "exec_real", fake_real)
    monkeypatch.setenv("OBSIDIAN_LAUNCHER", "/missing/obsidian")
    monkeypatch.setenv("XDG_OPEN_REAL", "/usr/bin/true")
    with pytest.raises(SystemExit):
        # When the forwarder runs
        forwarder.main(["obsidian://show-plugin?id=obsidian-importer"])
    # Then the real xdg-open receives the original argument
    assert calls == [
        (["obsidian://show-plugin?id=obsidian-importer"], "/usr/bin/true")
    ]


def test_obsidian_diagnostics_do_not_include_the_full_uri(monkeypatch, capsys) -> None:
    # Given a deep link carrying a private vault and file name

    def fake_app(argv: list[str], launcher: str) -> None:
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_app", fake_app)
    with pytest.raises(SystemExit):
        # When the forwarder runs
        forwarder.main(["obsidian://open?vault=private&file=secret-note"])
    # Then only the scheme and action are logged
    stderr = capsys.readouterr().err
    assert "scheme=obsidian" in stderr
    assert "secret-note" not in stderr
    assert "private" not in stderr


def test_send_posts_json() -> None:
    # Given a running daemon stub
    stub = StubServer()
    try:
        # When a URL is sent
        forwarder.send("https://example.com/a?b=1", endpoint=stub.endpoint)
        # Then it arrives as one JSON value
        assert stub.records == [
            {
                "path": "/api/open-url",
                "content_type": "application/json",
                "body": {"url": "https://example.com/a?b=1"},
            }
        ]
    finally:
        stub.close()


def test_send_raises_on_non_2xx() -> None:
    # Given a daemon stub that fails
    stub = StubServer(status=500)
    try:
        # When a URL is sent
        with pytest.raises(OSError):
            # Then the failure is reported
            forwarder.send("https://example.com", endpoint=stub.endpoint)
    finally:
        stub.close()


def test_send_preserves_shell_metacharacters(tmp_path: Path) -> None:
    # Given a URL containing shell metacharacters
    marker = tmp_path / "injection-marker"
    url = f"https://example.com/a;touch%20{marker}?x=$(id)"
    stub = StubServer()
    try:
        # When it is sent
        forwarder.send(url, endpoint=stub.endpoint)
    finally:
        stub.close()
    # Then it arrives as one JSON value and nothing was executed
    assert stub.records[0]["body"] == {"url": url}
    assert not marker.exists()


def test_main_forwards_without_exec(monkeypatch) -> None:
    # Given a valid URL and a running daemon
    stub = StubServer()
    monkeypatch.setenv("OPEN_URL_ENDPOINT", stub.endpoint)

    def fail(*args, **kwargs):
        raise AssertionError("exec_real must not be called")

    monkeypatch.setattr(forwarder, "exec_real", fail)
    try:
        # When the forwarder runs
        result = forwarder.main(["https://example.com/a"])
        # Then it forwards and returns success
        assert result == 0
        assert stub.records[0]["body"] == {"url": "https://example.com/a"}
    finally:
        stub.close()


def test_main_falls_back_on_connection_error(monkeypatch) -> None:
    # Given a valid URL and no daemon
    calls: list[tuple[list[str], str]] = []

    def fake_exec(argv: list[str], real: str) -> None:
        calls.append((list(argv), real))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_real", fake_exec)
    monkeypatch.setenv("OPEN_URL_ENDPOINT", "http://127.0.0.1:1/api/open-url")
    monkeypatch.setenv("XDG_OPEN_REAL", "/usr/bin/true")
    with pytest.raises(SystemExit):
        # When the forwarder runs
        forwarder.main(["https://example.com/a"])
    # Then it falls back to the real xdg-open with the original argument
    assert calls == [(["https://example.com/a"], "/usr/bin/true")]


def test_main_falls_back_for_non_url(monkeypatch) -> None:
    # Given a local file path
    calls: list[list[str]] = []

    def fake_exec(argv: list[str], real: str) -> None:
        calls.append(list(argv))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_real", fake_exec)
    with pytest.raises(SystemExit):
        # When the forwarder runs
        forwarder.main(["/config/a.pdf"])
    # Then the real xdg-open receives it unchanged
    assert calls == [["/config/a.pdf"]]


def test_diagnostics_do_not_include_the_full_url(monkeypatch, capsys) -> None:
    # Given a URL with a sensitive path and query
    stub = StubServer()
    monkeypatch.setenv("OPEN_URL_ENDPOINT", stub.endpoint)
    try:
        # When the forwarder runs
        forwarder.main(["https://example.com/secret-path?token=abc"])
    finally:
        stub.close()
    # Then only the scheme and host are logged
    stderr = capsys.readouterr().err
    assert "host=example.com" in stderr
    assert "secret-path" not in stderr
    assert "token=abc" not in stderr


def test_launcher_forwards_to_the_daemon(tmp_path: Path) -> None:
    # Given a running daemon stub and a real-binary stub
    stub = StubServer()
    real = _write_real_stub(tmp_path)
    marker = tmp_path / "real-xdg-open-args"
    env = {
        **os.environ,
        "OPEN_URL_ENDPOINT": stub.endpoint,
        "XDG_OPEN_REAL": str(real),
    }
    try:
        # When the launcher runs with a valid URL
        result = subprocess.run(
            [str(LAUNCHER), "https://example.com/launch"],
            env=env,
            capture_output=True,
            timeout=10,
        )
    finally:
        stub.close()
    # Then the daemon received it and the real binary was not called
    assert result.returncode == 0
    assert stub.records[0]["body"] == {"url": "https://example.com/launch"}
    assert not marker.exists()


def test_launcher_routes_an_obsidian_uri_to_the_app(tmp_path: Path) -> None:
    # Given an app-launcher stub, a real-binary stub and a dead daemon
    app = tmp_path / "obsidian-stub"
    app_marker = tmp_path / "obsidian-args"
    app.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{app_marker}'\n")
    app.chmod(0o755)
    real = _write_real_stub(tmp_path)
    env = {
        **os.environ,
        "OBSIDIAN_LAUNCHER": str(app),
        "XDG_OPEN_REAL": str(real),
        "OPEN_URL_ENDPOINT": "http://127.0.0.1:1/api/open-url",
    }
    # When the launcher runs with a single Obsidian deep link
    result = subprocess.run(
        [str(LAUNCHER), "obsidian://show-plugin?id=obsidian-importer"],
        env=env,
        capture_output=True,
        timeout=10,
    )
    # Then the app receives it and the real xdg-open is not called
    assert result.returncode == 0
    assert (
        app_marker.read_text().strip()
        == "obsidian://show-plugin?id=obsidian-importer"
    )
    assert not (tmp_path / "real-xdg-open-args").exists()


def test_launcher_falls_back_when_the_daemon_is_down(tmp_path: Path) -> None:
    # Given a dead daemon endpoint and a real-binary stub
    real = _write_real_stub(tmp_path)
    marker = tmp_path / "real-xdg-open-args"
    env = {
        **os.environ,
        "OPEN_URL_ENDPOINT": "http://127.0.0.1:1/api/open-url",
        "XDG_OPEN_REAL": str(real),
    }
    # When the launcher runs with a valid URL
    result = subprocess.run(
        [str(LAUNCHER), "https://example.com/fallback"],
        env=env,
        capture_output=True,
        timeout=10,
    )
    # Then the real binary receives the original argument
    assert result.returncode == 0
    assert marker.read_text().strip() == "https://example.com/fallback"
