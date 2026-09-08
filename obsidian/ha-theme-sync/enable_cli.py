"""Enable the Obsidian command line interface at boot.

The app gates every CLI command on the "cli" key of its global config
(<home>/.config/obsidian/obsidian.json). The theme daemon relies on the CLI
(`obsidian reload`) to apply theme changes to a running app, so this script
merges the key into the config before the app first reads it. Existing user
keys are preserved and an explicit user value for "cli" is left untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile

DEFAULT_HOME = "/config"
CONFIG_FILE_NAME = "obsidian.json"
DEFAULTS = {"frame": "native", "updateDisabled": True, "cli": True}


def _chown(path: str, uid: int | None, gid: int | None) -> None:
    if uid is None or gid is None:
        return
    with contextlib.suppress(OSError):
        os.chown(path, uid, gid)


def ensure_cli_enabled(home: str = DEFAULT_HOME) -> str:
    """Merge the CLI defaults into the Obsidian global config atomically.

    Returns the config file path.
    """
    config_dir = os.path.join(home, ".config", "obsidian")
    path = os.path.join(config_dir, CONFIG_FILE_NAME)
    os.makedirs(config_dir, mode=0o755, exist_ok=True)
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    for key, value in DEFAULTS.items():
        data.setdefault(key, value)
    descriptor, tmp_path = tempfile.mkstemp(prefix=".obsidian-", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return path


def main() -> None:
    path = ensure_cli_enabled()
    try:
        import pwd

        entry = pwd.getpwnam("abc")
        _chown(os.path.dirname(path), entry.pw_uid, entry.pw_gid)
        _chown(path, entry.pw_uid, entry.pw_gid)
    except (ImportError, KeyError):
        pass


if __name__ == "__main__":
    main()
