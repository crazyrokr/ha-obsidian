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
EMAIL_LAUNCHER = ROOT / "obsidian" / "root" / "usr" / "local" / "bin" / "xdg-email"
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
        (["mailto:a@b.c"], True),
        (["mailto:?subject=Hi"], True),
        (["mailto:"], False),
        (["tel:+15551234567"], True),
        (["tel:"], False),
        (["sms:+15551234567?body=Hi"], True),
        (["sms:"], False),
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


def test_main_email_mode_uses_the_email_fallback(monkeypatch) -> None:
    # Given a mail link and an unreachable daemon in email mode
    calls: list[tuple[list[str], str]] = []

    def fake_real(argv: list[str], real: str) -> None:
        calls.append((list(argv), real))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_real", fake_real)
    monkeypatch.setenv("OPEN_URL_MODE", "email")
    monkeypatch.setenv("XDG_EMAIL_REAL", "/usr/bin/xdg-email-stub")
    monkeypatch.setenv("OPEN_URL_ENDPOINT", "http://127.0.0.1:1/api/open-url")
    with pytest.raises(SystemExit):
        # When the email forwarder runs
        forwarder.main(["mailto:a@example.com"])
    # Then it falls back to the real xdg-email, not xdg-open
    assert calls == [(["mailto:a@example.com"], "/usr/bin/xdg-email-stub")]


def test_main_email_mode_falls_back_for_options(monkeypatch) -> None:
    # Given another tool calling xdg-email with options instead of a URI
    calls: list[tuple[list[str], str]] = []

    def fake_real(argv: list[str], real: str) -> None:
        calls.append((list(argv), real))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_real", fake_real)
    monkeypatch.setenv("OPEN_URL_MODE", "email")
    monkeypatch.setenv("XDG_EMAIL_REAL", "/usr/bin/xdg-email-stub")
    with pytest.raises(SystemExit):
        # When the email forwarder runs
        forwarder.main(["--subject", "Hi", "--body", "There"])
    # Then every option reaches the real binary unchanged
    assert calls == [
        (["--subject", "Hi", "--body", "There"], "/usr/bin/xdg-email-stub")
    ]


def test_main_email_mode_does_not_launch_the_obsidian_app(monkeypatch) -> None:
    # Given an obsidian URI arriving through xdg-email
    calls: list[tuple[list[str], str]] = []

    def fail(*args, **kwargs):
        raise AssertionError("the app must not be launched from xdg-email")

    def fake_real(argv: list[str], real: str) -> None:
        calls.append((list(argv), real))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "exec_app", fail)
    monkeypatch.setattr(forwarder, "exec_real", fake_real)
    monkeypatch.setenv("OPEN_URL_MODE", "email")
    monkeypatch.setenv("XDG_EMAIL_REAL", "/usr/bin/xdg-email-stub")
    with pytest.raises(SystemExit):
        # When the email forwarder runs
        forwarder.main(["obsidian://open?vault=main"])
    # Then it keeps the unchanged xdg-email fallback
    assert calls == [(["obsidian://open?vault=main"], "/usr/bin/xdg-email-stub")]


def test_mailto_diagnostics_do_not_include_the_recipient(monkeypatch, capsys) -> None:
    # Given a mail link with a private recipient and subject

    def fake_send(url: str, endpoint: str | None = None, timeout: float | None = None):
        return None

    monkeypatch.setattr(forwarder, "send", fake_send)
    # When the forwarder runs
    forwarder.main(["mailto:secret@example.com?subject=private"])
    # Then only the scheme is logged
    stderr = capsys.readouterr().err
    assert "scheme=mailto" in stderr
    assert "secret@example.com" not in stderr
    assert "private" not in stderr


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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["https://example.com/a"], "forward"),
        (["http://example.com"], "forward"),
        (["HTTPS://EXAMPLE.COM"], "forward"),
        (["obsidian://show-plugin?id=obsidian-importer"], "app"),
        (["file:///config/note.pdf"], "fallback"),
        (["/config/note.pdf"], "fallback"),
        (["mailto:user@example.com?subject=Hi"], "forward"),
        (["mailto:"], "fallback"),
        (["tel:+15551234567"], "forward"),
        (["sms:+15551234567"], "forward"),
        (["ftp://example.com/file"], "fallback"),
        (["zotero://select/library/items/ABCD"], "fallback"),
        (["javascript:alert(1)"], "fallback"),
        (["data:text/html,<h1>x</h1>"], "fallback"),
        (["about:blank"], "fallback"),
        (["app://obsidian.md/index.html"], "fallback"),
        (["obsidian://"], "fallback"),
        (["OBSIDIAN://show-plugin?id=x"], "fallback"),
        (["https://a", "https://b"], "fallback"),
        ([], "fallback"),
    ],
)
def test_main_routes_each_link_class(monkeypatch, argv, expected) -> None:
    # Given one link class and stubs for every destination
    routes: list[tuple[str, list[str]]] = []

    def fake_send(
        url: str, endpoint: str | None = None, timeout: float | None = None
    ) -> None:
        routes.append(("forward", [url]))

    def fake_app(args: list[str], launcher: str) -> None:
        routes.append(("app", list(args)))
        raise SystemExit(0)

    def fake_real(args: list[str], real: str) -> None:
        routes.append(("fallback", list(args)))
        raise SystemExit(0)

    monkeypatch.setattr(forwarder, "send", fake_send)
    monkeypatch.setattr(forwarder, "exec_app", fake_app)
    monkeypatch.setattr(forwarder, "exec_real", fake_real)
    # When the forwarder runs
    try:
        forwarder.main(list(argv))
    except SystemExit:
        pass
    # Then exactly the documented destination receives the original argument
    assert routes == [(expected, list(argv))]


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


@pytest.mark.parametrize(
    "link",
    ["tel:+15551234567", "sms:+15551234567?body=Hi"],
)
def test_launcher_forwards_phone_links(tmp_path: Path, link: str) -> None:
    # Given a running daemon stub and a real-binary stub
    stub = StubServer()
    real = _write_real_stub(tmp_path)
    env = {
        **os.environ,
        "OPEN_URL_ENDPOINT": stub.endpoint,
        "XDG_OPEN_REAL": str(real),
    }
    try:
        # When the launcher runs with a phone link
        result = subprocess.run(
            [str(LAUNCHER), link], env=env, capture_output=True, timeout=10
        )
    finally:
        stub.close()
    # Then the daemon received it and the real binary was not called
    assert result.returncode == 0
    assert stub.records[0]["body"] == {"url": link}
    assert not (tmp_path / "real-xdg-open-args").exists()


def test_email_launcher_forwards_mailto(tmp_path: Path) -> None:
    # Given a running daemon stub and a real xdg-email stub
    stub = StubServer()
    email_real = tmp_path / "xdg-email-stub"
    email_marker = tmp_path / "real-xdg-email-args"
    email_real.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{email_marker}'\n")
    email_real.chmod(0o755)
    env = {
        **os.environ,
        "OPEN_URL_ENDPOINT": stub.endpoint,
        "XDG_EMAIL_REAL": str(email_real),
    }
    try:
        # When the email launcher runs with a mail link
        result = subprocess.run(
            [str(EMAIL_LAUNCHER), "mailto:a@example.com?subject=Hi"],
            env=env,
            capture_output=True,
            timeout=10,
        )
    finally:
        stub.close()
    # Then the daemon received it and the real binary was not called
    assert result.returncode == 0
    assert stub.records[0]["body"] == {"url": "mailto:a@example.com?subject=Hi"}
    assert not email_marker.exists()


def test_email_launcher_falls_back_when_the_daemon_is_down(tmp_path: Path) -> None:
    # Given a dead daemon endpoint and a real xdg-email stub
    email_real = tmp_path / "xdg-email-stub"
    email_marker = tmp_path / "real-xdg-email-args"
    email_real.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{email_marker}'\n")
    email_real.chmod(0o755)
    env = {
        **os.environ,
        "OPEN_URL_ENDPOINT": "http://127.0.0.1:1/api/open-url",
        "XDG_EMAIL_REAL": str(email_real),
    }
    # When the email launcher runs with a mail link
    result = subprocess.run(
        [str(EMAIL_LAUNCHER), "mailto:a@example.com"],
        env=env,
        capture_output=True,
        timeout=10,
    )
    # Then the real xdg-email receives the original argument
    assert result.returncode == 0
    assert email_marker.read_text().strip() == "mailto:a@example.com"


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
