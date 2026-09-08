"""HTTP daemon that applies a requested theme to the active Obsidian vault.

Listens on the loopback interface and accepts POST /api/set-theme?theme=<name>.
The theme is written atomically into <vault>/.obsidian/appearance.json and the
running app is asked to reload through its system CLI, so the change takes
effect without restarting the add-on. The vault is resolved per request:
explicit override, most recent vault recorded in Obsidian's global config,
then the default vault location.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

VALID_THEMES = frozenset({"obsidian", "moonstone"})
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8090
DEFAULT_OBSIDIAN_HOME = "/config"
DEFAULT_VAULT = "/config"
VAULT_ENV_VAR = "THEME_SYNC_VAULT"
DEFAULT_CLI_COMMAND = ("obsidian", "reload")
DEFAULT_CLI_TIMEOUT = 15.0

_RGB_FN = re.compile(
    r"^rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*"
    r"(?:,\s*(\d+(?:\.\d+)?)\s*)?\)$"
)


def parse_color(value: str | None) -> tuple[float, float, float, float] | None:
    """Parse a CSS color into (r, g, b, alpha); return None when not a color.

    Accepts #rgb, #rrggbb, #rrggbbaa, rgb(...) and rgba(...) forms.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        if len(digits) not in (6, 8):
            return None
        try:
            channels = [int(digits[i : i + 2], 16) for i in range(0, 6, 2)]
        except ValueError:
            return None
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
        return (channels[0], channels[1], channels[2], alpha)
    match = _RGB_FN.match(text)
    if not match:
        return None
    channels = [float(match.group(index)) for index in range(1, 4)]
    alpha = float(match.group(4)) if match.group(4) is not None else 1.0
    if any(channel > 255 for channel in channels) or alpha > 1:
        return None
    return (channels[0], channels[1], channels[2], alpha)


def relative_luminance(color: tuple[float, float, float, float]) -> float:
    """WCAG relative luminance of an (r, g, b, a) color in 0-255 channels."""

    def channel(value: float) -> float:
        scaled = value / 255
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue, _ = color
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def is_dark_color(value: str | None, threshold: float = 0.5) -> bool | None:
    """Classify a CSS color as dark or light; None when the value is not a color."""
    color = parse_color(value)
    if color is None:
        return None
    return relative_luminance(color) < threshold


def resolve_vault(
    env: dict,
    obsidian_home: str = DEFAULT_OBSIDIAN_HOME,
    default_vault: str = DEFAULT_VAULT,
) -> str:
    """Resolve the vault directory for a request.

    Order: THEME_SYNC_VAULT override, most recent vault recorded in Obsidian's
    global config, then the default vault location.
    """
    override = env.get(VAULT_ENV_VAR)
    if override and os.path.isdir(override):
        return override
    global_config = os.path.join(obsidian_home, ".config", "obsidian", "obsidian.json")
    try:
        with open(global_config, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        newest: tuple[float, str] | None = None
        for entry in (data.get("vaults") or {}).values():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not os.path.isdir(path):
                continue
            stamp = entry.get("ts")
            if not isinstance(stamp, (int, float)):
                stamp = 0
            if newest is None or stamp > newest[0]:
                newest = (stamp, path)
        if newest is not None:
            return newest[1]
    return default_vault


def resolve_owner(env: dict) -> tuple[int | None, int | None]:
    """Owner (uid, gid) for written files: PUID/PGID, then the abc account."""
    try:
        return int(env["PUID"]), int(env["PGID"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        import pwd

        entry = pwd.getpwnam("abc")
        return entry.pw_uid, entry.pw_gid
    except (ImportError, KeyError):
        return None, None


def _chown(path: str, uid: int | None, gid: int | None) -> None:
    if uid is None or gid is None:
        return
    with contextlib.suppress(OSError):
        os.chown(path, uid, gid)


def apply_theme(
    vault: str, theme: str, uid: int | None = None, gid: int | None = None
) -> bool:
    """Set the theme in <vault>/.obsidian/appearance.json via an atomic rename.

    Returns True when the file changed, False when it already had the theme.
    Raises ValueError for an unknown theme.
    """
    if theme not in VALID_THEMES:
        raise ValueError(f"unsupported theme: {theme!r}")
    config_dir = os.path.join(vault, ".obsidian")
    os.makedirs(config_dir, mode=0o755, exist_ok=True)
    _chown(config_dir, uid, gid)
    path = os.path.join(config_dir, "appearance.json")
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    if data.get("theme") == theme:
        return False
    data["theme"] = theme
    descriptor, tmp_path = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_path, 0o644)
        _chown(tmp_path, uid, gid)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return True


def reload_obsidian(
    env: dict | None = None,
    command: tuple[str, ...] = DEFAULT_CLI_COMMAND,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> bool:
    """Ask the running Obsidian app to reload through its system CLI.

    Best-effort: returns True when the CLI exits successfully, False when the
    CLI is missing, the app is not running, or the call times out. Never
    raises, so a failed reload cannot turn a successful theme write into an
    error response.
    """
    base = dict(os.environ if env is None else env)
    base.setdefault("HOME", DEFAULT_OBSIDIAN_HOME)
    base.setdefault("XDG_RUNTIME_DIR", os.path.join(base["HOME"], ".XDG"))
    try:
        subprocess.run(
            list(command),
            check=True,
            timeout=timeout,
            env=base,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def handle_set_theme(
    theme: str | None,
    env: dict | None = None,
    reloder: Callable[..., object] | None = None,
) -> dict:
    """Apply a requested theme to the resolved vault and reload the app.

    Returns {"changed": bool, "reloaded": bool}. Raises ValueError for a
    missing or unknown theme, OSError on write failure. The reload is
    best-effort: a failure is reported as reloaded=False, never raised.
    """
    environment = dict(os.environ) if env is None else env
    if theme is None:
        raise ValueError("missing theme")
    vault = resolve_vault(environment)
    uid, gid = resolve_owner(environment)
    changed = apply_theme(vault, theme, uid, gid)
    if not changed:
        return {"changed": False, "reloaded": False}
    reload = reloder if reloder is not None else reload_obsidian
    try:
        reloaded = bool(reload(env=environment))
    except Exception:  # noqa: BLE001 - reload is best-effort by contract
        reloaded = False
    return {"changed": True, "reloaded": reloaded}


class ThemeRequestHandler(BaseHTTPRequestHandler):
    server_version = "ha-theme-sync"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        route = urlparse(self.path)
        if route.path.rstrip("/") != "/api/set-theme":
            self._respond(404, {"status": "error", "error": "not found"})
            return
        theme = parse_qs(route.query).get("theme", [None])[0]
        try:
            result = handle_set_theme(theme)
        except ValueError as error:
            self._respond(400, {"status": "error", "error": str(error)})
            return
        except OSError as error:
            self._respond(500, {"status": "error", "error": str(error)})
            return
        self._respond(200, {"status": "ok", "theme": theme, **result})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = os.environ.get("THEME_SYNC_HOST", DEFAULT_BIND_HOST)
    port = int(os.environ.get("THEME_SYNC_PORT", str(DEFAULT_BIND_PORT)))
    server = ThreadingHTTPServer((host, port), ThemeRequestHandler)
    print(f"[ha-theme-sync] listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
