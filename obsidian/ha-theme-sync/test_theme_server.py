"""Tests for the HA theme sync daemon (theme_server.py).

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import theme_server as ts  # noqa: E402


# --------------------------------------------------------------------------
# parse_color
# --------------------------------------------------------------------------

class TestParseColor:
    def test_short_hex(self) -> None:
        # Given a 3-digit hex color
        value = "#111"
        # When parsed
        result = ts.parse_color(value)
        # Then it expands to full channels
        assert result == (17.0, 17.0, 17.0, 1.0)

    def test_full_hex(self) -> None:
        # Given a 6-digit hex color
        value = "#112233"
        # When parsed
        result = ts.parse_color(value)
        # Then each channel is decoded
        assert result == (17.0, 34.0, 51.0, 1.0)

    def test_hex_with_alpha(self) -> None:
        # Given an 8-digit hex color
        value = "#11223344"
        # When parsed
        result = ts.parse_color(value)
        # Then the alpha channel is scaled to 0-1
        assert result == (17.0, 34.0, 51.0, 0x44 / 255)

    def test_uppercase_and_whitespace(self) -> None:
        # Given a color with surrounding whitespace and uppercase digits
        value = "  #ABC "
        # When parsed
        result = ts.parse_color(value)
        # Then it is normalized
        assert result == (170.0, 187.0, 204.0, 1.0)

    def test_rgb_function(self) -> None:
        # Given an rgb() value with extra spacing
        value = "rgb( 17 , 17 , 17 )"
        # When parsed
        result = ts.parse_color(value)
        # Then it is decoded with default alpha
        assert result == (17.0, 17.0, 17.0, 1.0)

    def test_rgba_function(self) -> None:
        # Given an rgba() value (HA default light background)
        value = "rgba(255, 255, 255, 0.8)"
        # When parsed
        result = ts.parse_color(value)
        # Then channels and alpha are decoded
        assert result == (255.0, 255.0, 255.0, 0.8)

    def test_float_channels(self) -> None:
        # Given float channel values
        value = "rgba(10.5, 20.5, 30.5, 0.5)"
        # When parsed
        result = ts.parse_color(value)
        # Then floats are preserved
        assert result == (10.5, 20.5, 30.5, 0.5)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            42,
            "",
            "   ",
            "not-a-color",
            "#12",
            "#12345",
            "#GGGGGG",
            "#123456789",
            "rgb(256, 0, 0)",
            "rgb(255, 255)",
            "rgb()",
            "rgba(0, 0, 0, 2)",
            "hsl(120, 50%, 50%)",
        ],
    )
    def test_invalid_values(self, value: object) -> None:
        # Given a value that is not a supported CSS color
        # When parsed
        result = ts.parse_color(value)  # type: ignore[arg-type]
        # Then it is rejected
        assert result is None


# --------------------------------------------------------------------------
# is_dark_color
# --------------------------------------------------------------------------

class TestIsDarkColor:
    def test_ha_default_dark_background(self) -> None:
        # Given the HA default dark theme background color
        value = "#111111"
        # When classified with the default threshold
        result = ts.is_dark_color(value)
        # Then it is dark
        assert result is True

    def test_ha_default_light_background(self) -> None:
        # Given the HA default light theme background color
        value = "rgba(255, 255, 255, 0.8)"
        # When classified with the default threshold
        result = ts.is_dark_color(value)
        # Then it is light
        assert result is False

    def test_mid_gray_is_dark(self) -> None:
        # Given a mid-gray background (luminance ~0.216)
        value = "#808080"
        # When classified with the default threshold of 0.5
        result = ts.is_dark_color(value)
        # Then it is below the threshold
        assert result is True

    def test_threshold_boundary_below(self) -> None:
        # Given a gray just below the 0.5 luminance boundary (~0.497)
        value = "#bbbbbb"
        # When classified
        result = ts.is_dark_color(value)
        # Then it is dark
        assert result is True

    def test_threshold_boundary_above(self) -> None:
        # Given a gray just above the 0.5 luminance boundary (~0.503)
        value = "#bcbcbc"
        # When classified
        result = ts.is_dark_color(value)
        # Then it is light
        assert result is False

    def test_custom_threshold(self) -> None:
        # Given a mid-gray background (luminance ~0.216)
        value = "#808080"
        # When classified against a stricter threshold
        assert ts.is_dark_color(value, threshold=0.1) is False
        # And against a looser threshold
        assert ts.is_dark_color(value, threshold=0.3) is True

    @pytest.mark.parametrize("value", [None, "", "garbage"])
    def test_unparseable_value(self, value: object) -> None:
        # Given a value that cannot be parsed as a color
        # When classified
        result = ts.is_dark_color(value)  # type: ignore[arg-type]
        # Then no decision is made
        assert result is None


# --------------------------------------------------------------------------
# resolve_vault
# --------------------------------------------------------------------------

def write_global_config(home: Path, payload: object) -> None:
    config_dir = home / ".config" / "obsidian"
    config_dir.mkdir(parents=True)
    (config_dir / "obsidian.json").write_text(
        "not json" if payload is Ellipsis else json.dumps(payload), encoding="utf-8"
    )


class TestResolveVault:
    def test_env_override_wins(self, tmp_path: Path) -> None:
        # Given a THEME_SYNC_VAULT override pointing at an existing directory
        vault = tmp_path / "vault"
        vault.mkdir()
        # When resolved
        result = ts.resolve_vault({"THEME_SYNC_VAULT": str(vault)}, str(tmp_path), str(tmp_path / "default"))
        # Then the override is used
        assert result == str(vault)

    def test_missing_override_falls_through(self, tmp_path: Path) -> None:
        # Given an override that does not exist and a valid global config
        recorded = tmp_path / "recorded"
        recorded.mkdir()
        write_global_config(tmp_path, {"vaults": {"a": {"path": str(recorded), "ts": 1}}})
        # When resolved
        result = ts.resolve_vault(
            {"THEME_SYNC_VAULT": str(tmp_path / "absent")}, str(tmp_path), str(tmp_path / "default")
        )
        # Then the recorded vault is used
        assert result == str(recorded)

    def test_newest_recorded_vault_wins(self, tmp_path: Path) -> None:
        # Given two recorded vaults with different timestamps
        older = tmp_path / "older"
        newer = tmp_path / "newer"
        older.mkdir()
        newer.mkdir()
        write_global_config(
            tmp_path,
            {
                "vaults": {
                    "a": {"path": str(older), "ts": 100},
                    "b": {"path": str(newer), "ts": 200},
                }
            },
        )
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the most recently used vault is selected
        assert result == str(newer)

    def test_recorded_vault_without_timestamp(self, tmp_path: Path) -> None:
        # Given a recorded vault without a timestamp
        vault = tmp_path / "vault"
        vault.mkdir()
        write_global_config(tmp_path, {"vaults": {"a": {"path": str(vault)}}})
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then it is still selected
        assert result == str(vault)

    def test_missing_recorded_path_is_skipped(self, tmp_path: Path) -> None:
        # Given a recorded vault whose path no longer exists
        write_global_config(
            tmp_path, {"vaults": {"a": {"path": str(tmp_path / "gone"), "ts": 5}}}
        )
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the default location is used
        assert result == str(tmp_path / "default")

    def test_corrupt_global_config(self, tmp_path: Path) -> None:
        # Given a global config file that is not valid JSON
        write_global_config(tmp_path, Ellipsis)
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the default location is used
        assert result == str(tmp_path / "default")

    def test_non_object_global_config(self, tmp_path: Path) -> None:
        # Given a global config that is a JSON list instead of an object
        write_global_config(tmp_path, [{"path": str(tmp_path)}])
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the default location is used
        assert result == str(tmp_path / "default")

    def test_malformed_vault_entries_are_skipped(self, tmp_path: Path) -> None:
        # Given vault entries that are not objects or have no path
        vault = tmp_path / "vault"
        vault.mkdir()
        write_global_config(
            tmp_path,
            {"vaults": {"a": "junk", "b": {"ts": 9}, "c": {"path": str(vault), "ts": 1}}},
        )
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the only valid entry is selected
        assert result == str(vault)

    def test_missing_global_config(self, tmp_path: Path) -> None:
        # Given no global config at all
        # When resolved
        result = ts.resolve_vault({}, str(tmp_path), str(tmp_path / "default"))
        # Then the default location is used
        assert result == str(tmp_path / "default")


# --------------------------------------------------------------------------
# resolve_owner
# --------------------------------------------------------------------------

class TestResolveOwner:
    def test_puid_pgid_env(self) -> None:
        # Given PUID and PGID environment values
        env = {"PUID": "1000", "PGID": "1000"}
        # When resolved
        uid, gid = ts.resolve_owner(env)
        # Then they are used as integers
        assert (uid, gid) == (1000, 1000)

    def test_invalid_puid_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given an invalid PUID and no abc account available
        import pwd

        def missing(_name: str) -> pwd.struct_passwd:
            raise KeyError("abc")

        monkeypatch.setattr(pwd, "getpwnam", missing)
        # When resolved
        uid, gid = ts.resolve_owner({"PUID": "not-a-number"})
        # Then no owner is forced
        assert (uid, gid) == (None, None)

    def test_missing_pgid_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given only PUID set and no abc account available
        import pwd

        def missing(_name: str) -> pwd.struct_passwd:
            raise KeyError("abc")

        monkeypatch.setattr(pwd, "getpwnam", missing)
        # When resolved
        uid, gid = ts.resolve_owner({"PUID": "1000"})
        # Then no owner is forced
        assert (uid, gid) == (None, None)


# --------------------------------------------------------------------------
# apply_theme
# --------------------------------------------------------------------------

def appearance(vault: Path) -> Path:
    return vault / ".obsidian" / "appearance.json"


class TestApplyTheme:
    def test_creates_appearance_file(self, tmp_path: Path) -> None:
        # Given a vault without an .obsidian folder
        vault = tmp_path / "vault"
        # When the moonstone theme is applied
        changed = ts.apply_theme(str(vault), "moonstone")
        # Then the file is created with the theme set
        assert changed is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        # Given an existing appearance.json with unrelated settings
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(
            json.dumps({"theme": "obsidian", "baseFontSize": 16, "translucency": True}),
            encoding="utf-8",
        )
        # When the moonstone theme is applied
        changed = ts.apply_theme(str(vault), "moonstone")
        # Then only the theme key changes
        data = json.loads(appearance(vault).read_text(encoding="utf-8"))
        assert changed is True
        assert data == {"theme": "moonstone", "baseFontSize": 16, "translucency": True}

    def test_same_theme_is_noop(self, tmp_path: Path) -> None:
        # Given an appearance.json that already has the requested theme
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "obsidian"}), encoding="utf-8")
        # When the same theme is applied
        changed = ts.apply_theme(str(vault), "obsidian")
        # Then nothing is rewritten
        assert changed is False

    def test_switching_theme_updates_file(self, tmp_path: Path) -> None:
        # Given an appearance.json with the dark theme
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "obsidian"}), encoding="utf-8")
        # When the light theme is applied
        changed = ts.apply_theme(str(vault), "moonstone")
        # Then the file reflects the new theme
        assert changed is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    @pytest.mark.parametrize("theme", ["dark", "light", "system", "OBSIDIAN", ""])
    def test_invalid_theme_rejected(self, tmp_path: Path, theme: str) -> None:
        # Given a theme value that is not in the allowlist
        vault = tmp_path / "vault"
        vault.mkdir()
        # When applying it
        with pytest.raises(ValueError):
            ts.apply_theme(str(vault), theme)
        # Then no appearance file is created
        assert not appearance(vault).exists()

    def test_corrupt_appearance_file_replaced(self, tmp_path: Path) -> None:
        # Given an appearance.json containing invalid JSON
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text("{broken", encoding="utf-8")
        # When a theme is applied
        changed = ts.apply_theme(str(vault), "moonstone")
        # Then the file is replaced with valid JSON
        assert changed is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_non_object_appearance_file_replaced(self, tmp_path: Path) -> None:
        # Given an appearance.json that is a JSON array
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text("[1, 2, 3]", encoding="utf-8")
        # When a theme is applied
        changed = ts.apply_theme(str(vault), "obsidian")
        # Then the file becomes an object with the theme set
        assert changed is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8")) == {"theme": "obsidian"}

    def test_no_temp_file_left_behind(self, tmp_path: Path) -> None:
        # Given a fresh vault
        vault = tmp_path / "vault"
        # When a theme is applied
        ts.apply_theme(str(vault), "moonstone")
        # Then no temporary files remain in .obsidian
        leftovers = [p.name for p in (vault / ".obsidian").iterdir() if p.name.startswith(".appearance-")]
        assert leftovers == []

    def test_unwritable_owner_is_best_effort(self, tmp_path: Path) -> None:
        # Given owner ids that the current user cannot chown to
        vault = tmp_path / "vault"
        # When a theme is applied with foreign uid/gid
        changed = ts.apply_theme(str(vault), "moonstone", uid=12345, gid=12345)
        # Then the write still succeeds
        assert changed is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_unwritable_vault_raises(self, tmp_path: Path) -> None:
        # Given a vault path where writing must fail (a file in the way)
        blocker = tmp_path / "vault"
        blocker.write_text("file", encoding="utf-8")
        # When a theme is applied
        with pytest.raises(OSError):
            ts.apply_theme(str(blocker), "moonstone")
        # Then no appearance file was created
        assert not (blocker / "appearance.json").exists()


# --------------------------------------------------------------------------
# handle_set_theme
# --------------------------------------------------------------------------

class TestHandleSetTheme:
    def test_applies_to_resolved_vault(self, tmp_path: Path) -> None:
        # Given a vault provided through the environment override
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the theme is requested
        result = ts.handle_set_theme(
            "obsidian", env={"THEME_SYNC_VAULT": str(vault)}, reloder=lambda **kw: True
        )
        # Then it is written to that vault
        assert result == {"changed": True, "reloaded": True}
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "obsidian"

    def test_missing_theme_rejected(self, tmp_path: Path) -> None:
        # Given no theme value
        # When the request is handled
        with pytest.raises(ValueError, match="missing theme"):
            ts.handle_set_theme(None, env={"THEME_SYNC_VAULT": str(tmp_path)})
        # Then nothing is written
        assert not appearance(tmp_path).exists()

    def test_invalid_theme_rejected(self, tmp_path: Path) -> None:
        # Given a vault and an unknown theme
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the request is handled
        with pytest.raises(ValueError):
            ts.handle_set_theme("purple", env={"THEME_SYNC_VAULT": str(vault)})
        # Then nothing is written
        assert not appearance(vault).exists()

    def test_reload_only_when_changed(self, tmp_path: Path) -> None:
        # Given a fresh vault and a reloader that records its calls
        vault = tmp_path / "vault"
        vault.mkdir()
        calls: list[dict] = []
        env = {"THEME_SYNC_VAULT": str(vault)}

        def reloder(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        # When the theme is applied twice
        first = ts.handle_set_theme("obsidian", env=env, reloder=reloder)
        second = ts.handle_set_theme("obsidian", env=env, reloder=reloder)
        # Then the reloader runs only for the first (changed) write
        assert first == {"changed": True, "reloaded": True}
        assert second == {"changed": False, "reloaded": False}
        assert len(calls) == 1

    def test_reload_receives_environment(self, tmp_path: Path) -> None:
        # Given a vault and a reloader that captures its arguments
        vault = tmp_path / "vault"
        vault.mkdir()
        captured: dict = {}

        def reloder(env: dict) -> bool:
            captured.update(env)
            return True

        # When the theme is applied
        ts.handle_set_theme("moonstone", env={"THEME_SYNC_VAULT": str(vault)}, reloder=reloder)
        # Then the request environment is passed through
        assert captured["THEME_SYNC_VAULT"] == str(vault)

    def test_reload_failure_still_reports_change(self, tmp_path: Path) -> None:
        # Given a fresh vault and a reloader that fails
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the theme is applied
        result = ts.handle_set_theme(
            "moonstone", env={"THEME_SYNC_VAULT": str(vault)}, reloder=lambda **kw: False
        )
        # Then the change stands and the reload is reported as failed
        assert result == {"changed": True, "reloaded": False}
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_reload_exception_is_contained(self, tmp_path: Path) -> None:
        # Given a fresh vault and a reloader that raises unexpectedly
        vault = tmp_path / "vault"
        vault.mkdir()

        def reloder(**kw: object) -> bool:
            raise RuntimeError("cli exploded")

        # When the theme is applied
        result = ts.handle_set_theme("obsidian", env={"THEME_SYNC_VAULT": str(vault)}, reloder=reloder)
        # Then the change stands and the error is contained
        assert result == {"changed": True, "reloaded": False}
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "obsidian"


# --------------------------------------------------------------------------
# reload_obsidian
# --------------------------------------------------------------------------

class TestReloadObsidian:
    def test_successful_reload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI that exits successfully
        calls: list[dict] = []

        def fake_run(argv: list, **kwargs: object) -> object:
            calls.append({"argv": argv, **kwargs})
            return 0

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        result = ts.reload_obsidian(env={"THEME_SYNC_VAULT": "/vault"})
        # Then it is reported as reloaded
        assert result is True

    def test_invokes_default_cli_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI that exits successfully
        calls: list[dict] = []

        def fake_run(argv: list, **kwargs: object) -> object:
            calls.append({"argv": argv, **kwargs})
            return 0

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        ts.reload_obsidian(env={})
        # Then the default command is run with check enabled and a timeout
        assert calls[0]["argv"] == ["/opt/obsidian/obsidian-cli", "reload"]
        assert calls[0]["check"] is True
        assert calls[0]["timeout"] == ts.DEFAULT_CLI_TIMEOUT

    def test_env_defaults_to_obsidian_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given no environment supplied
        calls: list[dict] = []

        def fake_run(argv: list, **kwargs: object) -> object:
            calls.append({"argv": argv, **kwargs})
            return 0

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        ts.reload_obsidian(env={})
        # Then HOME and XDG_RUNTIME_DIR point at the add-on config
        env = calls[0]["env"]
        assert env["HOME"] == "/config"
        assert env["XDG_RUNTIME_DIR"] == "/config/.XDG"

    def test_env_preserves_existing_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given an environment that already defines HOME and XDG_RUNTIME_DIR
        calls: list[dict] = []

        def fake_run(argv: list, **kwargs: object) -> object:
            calls.append({"argv": argv, **kwargs})
            return 0

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        ts.reload_obsidian(env={"HOME": "/elsewhere", "XDG_RUNTIME_DIR": "/elsewhere/.XDG"})
        # Then the supplied values win
        env = calls[0]["env"]
        assert env["HOME"] == "/elsewhere"
        assert env["XDG_RUNTIME_DIR"] == "/elsewhere/.XDG"

    def test_custom_command_and_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI client with an explicit path and a short timeout
        calls: list[dict] = []

        def fake_run(argv: list, **kwargs: object) -> object:
            calls.append({"argv": argv, **kwargs})
            return 0

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        ts.reload_obsidian(env={}, command=("/opt/obsidian/obsidian-cli", "reload"), timeout=1.0)
        # Then the supplied command and timeout are used
        assert calls[0]["argv"] == ["/opt/obsidian/obsidian-cli", "reload"]
        assert calls[0]["timeout"] == 1.0

    def test_missing_cli_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI binary that does not exist
        def fake_run(argv: list, **kwargs: object) -> object:
            raise FileNotFoundError("obsidian")

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        result = ts.reload_obsidian(env={})
        # Then the failure is reported, not raised
        assert result is False

    def test_nonzero_exit_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI that exits with an error (app not running)
        def fake_run(argv: list, **kwargs: object) -> object:
            raise ts.subprocess.CalledProcessError(1, "obsidian")

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        result = ts.reload_obsidian(env={})
        # Then the failure is reported, not raised
        assert result is False

    def test_timeout_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a CLI that hangs
        def fake_run(argv: list, **kwargs: object) -> object:
            raise ts.subprocess.TimeoutExpired("obsidian", 15)

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        # When the app is asked to reload
        result = ts.reload_obsidian(env={})
        # Then the failure is reported, not raised
        assert result is False


# --------------------------------------------------------------------------
# init-obsidian-cli oneshot (s6-rc service)
# --------------------------------------------------------------------------

ONESHOT_RUN = (
    Path(__file__).resolve().parent.parent
    / "root/etc/s6-overlay/s6-rc.d/init-obsidian-cli/run"
)


def read_global_config(home: Path) -> dict:
    return json.loads((home / ".config" / "obsidian" / "obsidian.json").read_text(encoding="utf-8"))


def run_oneshot(home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ONESHOT_RUN)],
        env={**os.environ, "OBSIDIAN_HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )


class TestInitObsidianCliOneshot:
    def test_creates_config_with_cli_enabled(self, tmp_path: Path) -> None:
        # Given a fresh home directory without any Obsidian config
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then the global config exists with the CLI enabled
        data = read_global_config(tmp_path)
        assert data["cli"] is True
        assert data["frame"] == "native"
        assert data["updateDisabled"] is True

    def test_preserves_existing_user_keys(self, tmp_path: Path) -> None:
        # Given an existing global config with user settings
        write_global_config(
            tmp_path,
            {"frame": "frameless", "updateDisabled": False, "vaults": {"a": 1}},
        )
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then the user settings survive and only cli is added
        data = read_global_config(tmp_path)
        assert data["cli"] is True
        assert data["frame"] == "frameless"
        assert data["updateDisabled"] is False
        assert data["vaults"] == {"a": 1}

    def test_explicit_cli_false_is_respected(self, tmp_path: Path) -> None:
        # Given a user that explicitly disabled the CLI
        write_global_config(tmp_path, {"cli": False})
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then the explicit choice is not overridden
        assert read_global_config(tmp_path)["cli"] is False

    def test_corrupt_config_is_replaced(self, tmp_path: Path) -> None:
        # Given a global config that is not valid JSON
        write_global_config(tmp_path, Ellipsis)
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then the file is replaced with valid defaults
        assert read_global_config(tmp_path)["cli"] is True

    def test_non_object_config_is_replaced(self, tmp_path: Path) -> None:
        # Given a global config that is a JSON array
        write_global_config(tmp_path, [1, 2])
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then the file is replaced with an object carrying the defaults
        data = read_global_config(tmp_path)
        assert data["cli"] is True
        assert isinstance(data, dict)

    def test_idempotent(self, tmp_path: Path) -> None:
        # Given a config already written by a previous boot
        run_oneshot(tmp_path)
        config_path = tmp_path / ".config" / "obsidian" / "obsidian.json"
        first = config_path.read_text(encoding="utf-8")
        # When the oneshot runs again
        run_oneshot(tmp_path)
        # Then the content is unchanged
        assert config_path.read_text(encoding="utf-8") == first

    def test_no_temp_file_left_behind(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        # When the oneshot runs at boot
        run_oneshot(tmp_path)
        # Then no temporary files remain
        config_dir = tmp_path / ".config" / "obsidian"
        leftovers = [p.name for p in config_dir.iterdir() if p.name.startswith(".obsidian-")]
        assert leftovers == []

    def test_blocked_config_directory_fails(self, tmp_path: Path) -> None:
        # Given a home path where the config directory cannot be created (a file in the way)
        (tmp_path / ".config").write_text("file", encoding="utf-8")
        # When the oneshot runs at boot
        with pytest.raises(subprocess.CalledProcessError):
            run_oneshot(tmp_path)


# --------------------------------------------------------------------------
# HTTP interface
# --------------------------------------------------------------------------

@pytest.fixture()
def server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ts.ThemeRequestHandler)
    import threading

    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()


def post(server: ThreadingHTTPServer, path: str) -> tuple[int, dict]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("POST", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body)
    finally:
        connection.close()


class TestHttpApi:
    def test_set_theme_ok(self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a vault available through the environment and a working CLI
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: True)
        # When the API is called with a valid theme
        status, payload = post(server, "/api/set-theme?theme=moonstone")
        # Then it succeeds, the theme is written and the app is reloaded
        assert status == 200
        assert payload["status"] == "ok"
        assert payload["changed"] is True
        assert payload["reloaded"] is True
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_reload_failure_still_ok(self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a vault and a CLI that fails (app not running)
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: False)
        # When the API is called with a valid theme
        status, payload = post(server, "/api/set-theme?theme=moonstone")
        # Then the write stands and the failed reload is reported
        assert status == 200
        assert payload["status"] == "ok"
        assert payload["changed"] is True
        assert payload["reloaded"] is False
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_same_theme_reports_unchanged(self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a vault that already has the requested theme
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "obsidian"}), encoding="utf-8")
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        calls: list[dict] = []
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: calls.append(env) or True)
        # When the API is called with the same theme
        status, payload = post(server, "/api/set-theme?theme=obsidian")
        # Then it succeeds, reports no change and does not reload
        assert status == 200
        assert payload["changed"] is False
        assert payload["reloaded"] is False
        assert calls == []

    def test_invalid_theme_is_client_error(self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a vault and an unknown theme value
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        # When the API is called
        status, payload = post(server, "/api/set-theme?theme=purple")
        # Then it is a 400 with an error and nothing is written
        assert status == 400
        assert payload["status"] == "error"
        assert not appearance(vault).exists()

    def test_missing_theme_is_client_error(self, server: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Given a request without a theme parameter
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        # When the API is called
        status, payload = post(server, "/api/set-theme")
        # Then it is a 400
        assert status == 400
        assert payload["status"] == "error"

    def test_trailing_slash_accepted(self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a vault and a working CLI
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: True)
        # When the API path carries a trailing slash
        status, payload = post(server, "/api/set-theme/?theme=moonstone")
        # Then it still succeeds
        assert status == 200
        assert payload["status"] == "ok"

    def test_unknown_path_is_not_found(self, server: ThreadingHTTPServer) -> None:
        # Given a request to an unrelated path
        # When the API is called
        status, payload = post(server, "/api/other?theme=moonstone")
        # Then it is a 404
        assert status == 404
        assert payload["status"] == "error"
