"""HTTP daemon that applies a requested theme to the active Obsidian vault.

Listens on the loopback interface and accepts POST /api/set-theme?theme=<name>.
The theme is written atomically into <vault>/.obsidian/appearance.json and the
running app is asked to reload through its system CLI, so the change takes
effect without restarting the add-on. The vault is resolved per request:
explicit override, most recent vault recorded in Obsidian's global config,
then the default vault location.

A background watcher follows Obsidian's vault registry: a vault created after
the last request starts with Obsidian's default appearance, so the last
requested theme is applied to it and the app is reloaded — no browser request
needed. The watcher is event-driven on Linux (inotify on the config
directory, idle cost zero) and degrades to a few seconds of polling only
where inotify is unavailable.

The desktop background follows the theme as well. In the base image the
desktop is either an Xvfb server (X mode) or a labwc compositor nested
inside Selkies' headless Wayland compositor (Wayland mode); the daemon keeps
the background color in sync with the theme through xsetroot or swaybg
respectively, re-applying it until (and after) the desktop is up.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import select
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

VALID_THEMES = frozenset({"obsidian", "moonstone"})
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8090
DEFAULT_OBSIDIAN_HOME = "/config"
DEFAULT_VAULT = "/config"
VAULT_ENV_VAR = "THEME_SYNC_VAULT"
# The CLI client binary, not the `obsidian` on PATH: in the base image the
# latter is the app launcher, so a relative name would start a second app.
DEFAULT_CLI_COMMAND = ("/opt/obsidian/obsidian-cli", "reload")
DEFAULT_CLI_TIMEOUT = 15.0
# The last requested theme, persisted so the vault watcher can apply it to
# vaults created after the last request.
LAST_THEME_STATE_FILE = "ha-theme-sync.json"
DEFAULT_VAULT_WATCH_INTERVAL = 2.0
# Event mode slices the inotify wait so a stop request is honored within this
# bound; the sweep rescan guards against events that were never delivered.
DEFAULT_WAKE_TIMEOUT = 1.0
DEFAULT_SWEEP_INTERVAL = 60.0

# Default desktop background per theme, used when a request carries no color
# (standalone tab, or an older client). Black matches the stock desktop; the
# light value is HA's stock light-theme background.
DEFAULT_BACKGROUND = {"obsidian": "#000000", "moonstone": "#f2f4f9"}
# Background clients: swaybg paints the background layer of the nested labwc
# compositor (Wayland mode); xsetroot sets the root window of Xvfb (X mode).
DEFAULT_SWAYBG_COMMAND = ("/usr/bin/swaybg",)
DEFAULT_XSETROOT_COMMAND = ("/usr/bin/xsetroot",)
# How often the supervisor wakes: it restarts a dead background client (and
# retries until the compositor is up in Wayland mode) and re-applies the
# X root color on this cadence.
DEFAULT_BACKGROUND_RETRY_INTERVAL = 2.0
# X mode re-applies the root color on this cadence, so a restarted X server
# or a client that repaints the root cannot leave a stale background.
DEFAULT_BACKGROUND_REAPPLY_INTERVAL = 30.0

# inotify(7) event masks: the config file appears, is rewritten in place, or
# is swapped for a new inode. Obsidian (Electron) can do either — a temp file
# plus rename, or a plain in-place write — and the swap would kill a watch on
# the file itself, so we watch the directory, which survives and reports both
# cases.
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MODIFY = 0x00000002
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_DELETE_SELF = 0x00000400
IN_IGNORED = 0x00008000

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


def format_hex(color: tuple[float, float, float, float]) -> str:
    """Normalize a parsed (r, g, b, a) color to an opaque '#rrggbb' string."""
    channels = [max(0, min(255, round(channel))) for channel in color[:3]]
    return "#{:02x}{:02x}{:02x}".format(*channels)


def normalize_requested_background(theme: str, color: str | None) -> str:
    """Resolve the requested background color for a theme.

    A missing color falls back to the theme default. An explicit color must
    parse, and its darkness must agree with the theme — a light color with
    the dark theme (or vice versa) is a client contract violation, rejected
    with ValueError rather than painted.
    """
    if color is None:
        return DEFAULT_BACKGROUND[theme]
    parsed = parse_color(color)
    if parsed is None:
        raise ValueError(f"invalid color: {color!r}")
    if is_dark_color(color) != (theme == "obsidian"):
        raise ValueError(f"color {color!r} does not match theme {theme!r}")
    return format_hex(parsed)


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
    obsidian_home: str | None = None,
    color: str | None = None,
) -> dict:
    """Apply a requested theme (and optional background color) to the vault.

    Persists the requested theme and background so the vault watcher can
    bring newly created vaults in sync and the desktop background can be
    restored at boot. Returns {"changed": bool, "reloaded": bool,
    "background": str}. Raises ValueError for a missing or unknown theme, an
    invalid color, or a color whose darkness contradicts the theme; OSError
    on write failure. The reload is best-effort: a failure is reported as
    reloaded=False, never raised.
    """
    environment = dict(os.environ) if env is None else env
    home = obsidian_home or environment.get("OBSIDIAN_HOME") or DEFAULT_OBSIDIAN_HOME
    if theme is None:
        raise ValueError("missing theme")
    if theme not in VALID_THEMES:
        raise ValueError(f"unsupported theme: {theme!r}")
    background = normalize_requested_background(theme, color)
    vault = resolve_vault(environment, obsidian_home=home)
    uid, gid = resolve_owner(environment)
    changed = apply_theme(vault, theme, uid, gid)
    store_last_theme(theme, home, background=background)
    background_applied = apply_background(background)
    if not changed:
        return {
            "changed": False,
            "reloaded": False,
            "background": background,
            "background_applied": background_applied,
        }
    reload = reloder if reloder is not None else reload_obsidian
    try:
        reloaded = bool(reload(env=environment))
    except Exception:  # noqa: BLE001 - reload is best-effort by contract
        reloaded = False
    return {
        "changed": True,
        "reloaded": reloaded,
        "background": background,
        "background_applied": background_applied,
    }


def _read_state(home: str) -> dict:
    try:
        with open(os.path.join(home, ".config", LAST_THEME_STATE_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def store_last_theme(
    theme: str, home: str = DEFAULT_OBSIDIAN_HOME, background: str | None = None
) -> None:
    """Persist the last requested theme (and background) (best-effort).

    The theme is already applied when this runs, so a failure is swallowed:
    the watcher simply has nothing to apply until the next request. When
    `background` is None the previously stored background is kept, so a
    request that only carries a theme cannot clobber a remembered color.
    """
    if theme not in VALID_THEMES:
        return
    payload: dict = {"theme": theme}
    if background is not None:
        if parse_color(background) is not None:
            payload["background"] = background
    elif background is None:
        stored = _read_state(home).get("background")
        if isinstance(stored, str) and parse_color(stored) is not None:
            payload["background"] = stored
    path = os.path.join(home, ".config", LAST_THEME_STATE_FILE)
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
    except OSError:
        pass


def load_last_theme(home: str = DEFAULT_OBSIDIAN_HOME) -> str | None:
    """Read the persisted last requested theme; None when absent or invalid."""
    theme = _read_state(home).get("theme")
    return theme if theme in VALID_THEMES else None


def load_last_background(home: str = DEFAULT_OBSIDIAN_HOME) -> str | None:
    """Read the persisted background color; None when absent or not a color.

    The value is returned normalized to '#rrggbb' so the daemon can compare
    it against request colors without re-parsing.
    """
    value = _read_state(home).get("background")
    if not isinstance(value, str):
        return None
    parsed = parse_color(value)
    return format_hex(parsed) if parsed is not None else None


def registered_vaults(config_data: object) -> set[str]:
    """Existing vault paths recorded in Obsidian's global config."""
    if not isinstance(config_data, dict):
        return set()
    vaults = config_data.get("vaults")
    if not isinstance(vaults, dict):
        return set()
    paths: set[str] = set()
    for entry in vaults.values():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str) and os.path.isdir(path):
            paths.add(path)
    return paths


def _load_global_config(home: str) -> object:
    try:
        with open(
            os.path.join(home, ".config", "obsidian", "obsidian.json"), encoding="utf-8"
        ) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


class VaultWatcher:
    """Apply the remembered theme to vaults created after the last request.

    Obsidian records every vault it opens in its global config. A new entry
    means a vault that started with Obsidian's default appearance, so the
    remembered theme is applied to it and the running app is reloaded. The
    registry is re-read on every step; when to step is decided by
    run_vault_watcher (an inotify event on the config directory, with
    polling as the fallback).
    """

    def __init__(
        self,
        home: str = DEFAULT_OBSIDIAN_HOME,
        env: dict | None = None,
        reloder: Callable[..., object] | None = None,
    ) -> None:
        self.home = home
        self.environment = dict(os.environ) if env is None else dict(env)
        self.reloder = reloder if reloder is not None else reload_obsidian
        self.seen_vaults: set[str] | None = None

    def step(self) -> int:
        """Synchronize vaults registered since the previous step.

        The first step only records the current registry, so vaults that
        existed before the watcher started are never overwritten. Returns
        the number of vaults written.
        """
        current = registered_vaults(_load_global_config(self.home))
        if self.seen_vaults is None:
            self.seen_vaults = current
            return 0
        new_vaults = current - self.seen_vaults
        self.seen_vaults = current
        theme = load_last_theme(self.home)
        if not new_vaults or theme is None:
            return 0
        uid, gid = resolve_owner(self.environment)
        changed = 0
        for vault in sorted(new_vaults):
            try:
                if apply_theme(vault, theme, uid, gid):
                    changed += 1
            except OSError:
                continue
        if changed:
            try:
                self.reloder(env=self.environment)
            except Exception:  # noqa: BLE001 - reload is best-effort by contract
                pass
        return changed


class InotifyWaiter:
    """Wait for changes inside a directory with inotify(7); no CPU while idle.

    wait() blocks in a kernel select/read, so an idle watcher costs nothing.
    A delivered event is drained and never re-fires. When the watched
    directory itself is removed (IN_DELETE_SELF), is_alive turns False and
    the caller must fall back to polling or recreate the waiter.
    """

    def __init__(self, directory: str) -> None:
        libc = self._load_libc()
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.restype = ctypes.c_int
        ctypes.set_errno(0)
        fd = libc.inotify_init1(0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        try:
            ctypes.set_errno(0)
            mask = IN_CREATE | IN_DELETE | IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO
            watch = libc.inotify_add_watch(fd, os.fsencode(directory), mask)
        except BaseException:
            os.close(fd)
            raise
        if watch < 0:
            os.close(fd)
            raise OSError(ctypes.get_errno(), "inotify_add_watch failed")
        self._fd = fd
        self._alive = True

    @staticmethod
    def _load_libc() -> "ctypes.CDLL":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.inotify_init1
            libc.inotify_add_watch
            return libc
        except (AttributeError, OSError):
            raise OSError("inotify is not available on this platform") from None

    @property
    def is_alive(self) -> bool:
        return self._alive

    def wait(self, timeout: float) -> bool:
        """Block until a change event arrives; True on event, False on timeout.

        Raises nothing: a broken watch marks the waiter dead and returns
        False, so the caller always sees a boolean and a consistent
        is_alive.
        """
        if not self._alive:
            return False
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
            if not ready:
                return False
            data = os.read(self._fd, 65536)
        except OSError:
            self._alive = False
            return False
        if not data:
            self._alive = False
            return False
        offset = 0
        while offset + 16 <= len(data):
            # inotify_event: int32 wd, uint32 mask, uint32 cookie, uint32 len
            mask = struct.unpack_from("<I", data, offset + 4)[0]
            length = struct.unpack_from("<I", data, offset + 12)[0]
            # Either flag means the watched directory is gone: mark the
            # waiter dead so the caller falls back or recreates it.
            if mask & (IN_DELETE_SELF | IN_IGNORED):
                self._alive = False
            offset += 16 + length
        return True


def create_config_waiter(home: str) -> InotifyWaiter | None:
    """Event waiter for the directory holding Obsidian's global config.

    Returns None when inotify is unavailable or the directory does not exist
    yet. The watcher loop polls in that case and retries on every cycle, so a
    config directory that appears later is picked up without a restart.
    """
    try:
        return InotifyWaiter(os.path.join(home, ".config", "obsidian"))
    except OSError:
        return None


def run_vault_watcher(
    home: str = DEFAULT_OBSIDIAN_HOME,
    env: dict | None = None,
    reloder: Callable[..., object] | None = None,
    interval: float = DEFAULT_VAULT_WATCH_INTERVAL,
    stop: threading.Event | None = None,
    sweep: float = DEFAULT_SWEEP_INTERVAL,
    waiter: InotifyWaiter | None = None,
) -> None:
    """Synchronize vaults registered since the previous step, idly for free.

    On Linux the loop blocks in inotify until Obsidian's config directory
    changes, so an idle watcher costs no CPU and reacts within one syscall of
    the write. A sweep rescan every `sweep` seconds guards against events
    that were never delivered. Without inotify (or before the config
    directory exists) the loop polls every `interval` seconds instead.
    """
    watcher = VaultWatcher(home=home, env=env, reloder=reloder)

    def step() -> None:
        try:
            watcher.step()
        except Exception:  # noqa: BLE001 - the watcher must survive any hiccup
            pass

    stop_event = stop if stop is not None else threading.Event()
    # Baseline: the first step records the existing registry, so later steps
    # can tell new vaults apart from pre-existing ones.
    step()
    last_step = time.monotonic()
    while not stop_event.is_set():
        if waiter is None or not waiter.is_alive:
            # No live channel: scan once to cover the time the channel was
            # blind, then re-establish it before trusting events again.
            step()
            last_step = time.monotonic()
            waiter = create_config_waiter(home)
            if waiter is not None and waiter.is_alive:
                continue  # baseline covered; from here on, trust events
            stop_event.wait(interval)  # still no channel: poll next cycle
            continue
        now = time.monotonic()
        if now - last_step >= sweep:
            # Safety rescan in case an event was never delivered.
            step()
            last_step = time.monotonic()
            continue
        if not waiter.wait(min(DEFAULT_WAKE_TIMEOUT, sweep - (now - last_step))):
            continue  # timed out: loop top re-checks the sweep deadline
        # a config change arrived
        step()
        last_step = time.monotonic()


def scan_process_comms(proc_dir: str = "/proc") -> dict[int, str]:
    """Map every held file-inode to the comm of the process holding it.

    Covers both socket fds (`socket:[N]` → N) and regular-file fds (the
    target path's inode) — a Wayland server keeps its
    `wayland-<n>.lock` open as a regular file, so the lock's inode names
    its owner. Unreadable /proc entries (other users' processes) are
    skipped; the daemon and the desktop stack all run as the same add-on
    user, so the entries that matter are always visible.
    """
    mapping: dict[int, str] = {}
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return mapping
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = os.path.join(proc_dir, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        comm = ""
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link.startswith("socket:["):
                inode = int(link[len("socket:["):-1])
            else:
                try:
                    inode = os.stat(link).st_ino
                except OSError:
                    continue
            if inode in mapping:
                continue
            if not comm:
                try:
                    with open(os.path.join(proc_dir, entry, "comm"), encoding="ascii") as handle:
                        comm = handle.read().strip()
                except OSError:
                    comm = ""
            mapping[inode] = comm
    return mapping


def find_labwc_display(runtime_dir: str, proc_map: dict[int, str]) -> str | None:
    """Name of the Wayland display owned by a labwc server, if any.

    Every Wayland server keeps `<XDG_RUNTIME_DIR>/wayland-<n>.lock` open while
    it runs, so the lock's inode identifies its owner. In this image Selkies
    owns the first display (it is the compositor being streamed) and labwc
    runs nested inside it on the next free display — only a labwc-owned
    display will actually render a background client.
    """
    try:
        entries = sorted(os.listdir(runtime_dir))
    except OSError:
        return None
    for entry in entries:
        if not entry.startswith("wayland-") or not entry.endswith(".lock"):
            continue
        try:
            inode = os.stat(os.path.join(runtime_dir, entry)).st_ino
        except OSError:
            continue
        if proc_map.get(inode) == "labwc":
            return entry[: -len(".lock")]
    return None


class DesktopBackground:
    """Keep the desktop background color in sync with the requested theme.

    The base image ships two desktop stacks, selected by PIXELFLUX_WAYLAND:
    Xvfb on $DISPLAY (X mode), whose root window is painted with xsetroot,
    and labwc nested inside Selkies' headless compositor (Wayland mode),
    whose background layer is painted by a swaybg client of labwc.

    The supervisor thread re-establishes the background whenever it is
    missing: until the desktop is up (the spawn is deferred and retried),
    and afterwards whenever the background client dies or, in X mode, every
    reapply interval. All failures are swallowed — the background is a
    cosmetic concern and must never break a theme request.
    """

    def __init__(
        self,
        env: dict | None = None,
        swaybg_command: tuple[str, ...] = DEFAULT_SWAYBG_COMMAND,
        xsetroot_command: tuple[str, ...] = DEFAULT_XSETROOT_COMMAND,
        display: str | None = None,
        reapply_interval: float | None = None,
        proc_scan: Callable[[], dict[int, str]] = scan_process_comms,
    ) -> None:
        self.environment = dict(os.environ) if env is None else dict(env)
        self.swaybg_command = tuple(swaybg_command)
        self.xsetroot_command = tuple(xsetroot_command)
        self.wayland_display = display
        self.reapply_interval = (
            DEFAULT_BACKGROUND_REAPPLY_INTERVAL
            if reapply_interval is None
            else reapply_interval
        )
        self.proc_scan = proc_scan
        home = self.environment.get("HOME") or DEFAULT_OBSIDIAN_HOME
        self.runtime_dir = (
            self.environment.get("XDG_RUNTIME_DIR") or os.path.join(home, ".XDG")
        )
        self._is_wayland = self.environment.get("PIXELFLUX_WAYLAND", "").lower() == "true"
        self._target: str | None = None
        self._process: subprocess.Popen | None = None
        self._last_applied: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ state

    @property
    def target(self) -> str | None:
        with self._lock:
            return self._target

    def set_color(self, color: str) -> bool:
        """Adopt a background color; True when a client was (re)started now.

        A color equal to the current target is a no-op. When the desktop is
        not up yet the start is deferred to the supervisor, so False does
        not mean the color was rejected — only that it is not running yet.
        """
        with self._lock:
            if self._target == color:
                return False
            self._target = color
            return self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._target = None
            self._stop_process_locked()

    # -------------------------------------------------------- internals

    def _stop_process_locked(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.SubprocessError):
            process.wait(timeout=2)
        with contextlib.suppress(OSError):
            process.kill()

    def _start_locked(self) -> bool:
        self._stop_process_locked()
        if self._is_wayland:
            display = self._wayland_display()
            if display is None:
                return False  # desktop not up yet; the supervisor retries
            env = dict(self.environment)
            env.setdefault("HOME", DEFAULT_OBSIDIAN_HOME)
            env.setdefault("XDG_RUNTIME_DIR", self.runtime_dir)
            env["WAYLAND_DISPLAY"] = display
            command = (*self.swaybg_command, "-c", self._target)
            try:
                self._process = subprocess.Popen(
                    list(command),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
            self._last_applied = time.monotonic()
            return True
        # X mode: xsetroot is one-shot; success is recorded and the
        # supervisor re-applies on a cadence instead of tracking a process.
        env = dict(self.environment)
        env.setdefault("DISPLAY", ":1")
        command = (*self.xsetroot_command, "-solid", self._target)
        try:
            result = subprocess.run(
                list(command),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False  # X server not up yet; the supervisor retries
        self._last_applied = time.monotonic()
        return True

    def _wayland_display(self) -> str | None:
        if self.wayland_display is not None:
            return self.wayland_display
        return find_labwc_display(self.runtime_dir, self.proc_scan())

    def step(self) -> bool:
        """Ensure the background client is alive (supervisor entry point).

        True when the requested color is currently in effect. Never raises.
        """
        with self._lock:
            if self._target is None:
                return False
            now = time.monotonic()
            if self._is_wayland:
                if self._process is not None and self._process.poll() is None:
                    return True
                return self._start_locked()
            if (
                self._last_applied is not None
                and now - self._last_applied < self.reapply_interval
            ):
                return True
            return self._start_locked()


def run_background_supervisor(
    manager: DesktopBackground,
    stop: threading.Event | None = None,
    interval: float = DEFAULT_BACKGROUND_RETRY_INTERVAL,
) -> None:
    """Keep the desktop background alive until stopped.

    An idle supervisor wakes every `interval` seconds; each wake is a cheap
    poll of the background client. A step that raises is swallowed, so the
    supervisor can never die.
    """
    stop_event = stop if stop is not None else threading.Event()
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break
        try:
            manager.step()
        except Exception:  # noqa: BLE001 - the supervisor must survive anything
            pass


# The daemon installs one manager here from main(); handle_set_theme routes
# background requests through it, and tests substitute fakes.
_background_manager: DesktopBackground | None = None


def set_background_manager(manager: DesktopBackground | None) -> None:
    global _background_manager
    _background_manager = manager


def apply_background(color: str) -> bool:
    """Start or switch the desktop background to `color`; False when not.

    Never raises: a missing manager or a failing client only leaves the
    background stale, never the theme request in error.
    """
    if _background_manager is None:
        return False
    try:
        return _background_manager.set_color(color)
    except Exception:  # noqa: BLE001 - best-effort by contract
        return False


class ThemeRequestHandler(BaseHTTPRequestHandler):
    server_version = "ha-theme-sync"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        route = urlparse(self.path)
        if route.path.rstrip("/") != "/api/set-theme":
            self._respond(404, {"status": "error", "error": "not found"})
            return
        params = parse_qs(route.query)
        theme = params.get("theme", [None])[0]
        color = params.get("color", [None])[0]
        try:
            result = handle_set_theme(theme, color=color)
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
    home = os.environ.get("OBSIDIAN_HOME") or DEFAULT_OBSIDIAN_HOME
    # Restore the remembered desktop background once the desktop is up; the
    # supervisor retries until then and keeps the background client alive.
    background = DesktopBackground(env=os.environ)
    set_background_manager(background)
    remembered = load_last_background(home)
    if remembered is not None:
        background.set_color(remembered)
    background_supervisor = threading.Thread(
        target=run_background_supervisor,
        kwargs={"manager": background},
        name="ha-theme-sync-background",
        daemon=True,
    )
    background_supervisor.start()
    watcher = threading.Thread(
        target=run_vault_watcher,
        kwargs={"home": home},
        name="ha-theme-sync-vaults",
        daemon=True,
    )
    watcher.start()
    server = ThreadingHTTPServer((host, port), ThemeRequestHandler)
    print(f"[ha-theme-sync] listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
