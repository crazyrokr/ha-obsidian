"""Tests for the HA theme sync daemon (theme_server.py).

Every test follows the Given-When-Then structure.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "obsidian" / "root" / "opt" / "ha-theme-sync"))

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
    config_dir.mkdir(parents=True, exist_ok=True)
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


def state_file(home: Path) -> Path:
    return home / ".config" / "ha-theme-sync.json"


def register_vault(home: Path, vault: Path, index: str = "v") -> None:
    write_global_config(home, {"vaults": {index: {"path": str(vault), "ts": 1}}})


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
# last-theme state
# --------------------------------------------------------------------------


class TestLastThemeState:
    def test_store_and_load_roundtrip(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        # When a theme is stored
        ts.store_last_theme("moonstone", str(tmp_path))
        # Then the same theme is read back
        assert ts.load_last_theme(str(tmp_path)) == "moonstone"

    def test_stored_theme_is_plain_json(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        ts.store_last_theme("obsidian", str(tmp_path))
        # When the state file is read
        data = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        # Then it carries only the theme
        assert data == {"theme": "obsidian"}

    def test_store_overwrites_previous_theme(self, tmp_path: Path) -> None:
        # Given a stored theme
        ts.store_last_theme("moonstone", str(tmp_path))
        # When another theme is stored
        ts.store_last_theme("obsidian", str(tmp_path))
        # Then the newest one is read back
        assert ts.load_last_theme(str(tmp_path)) == "obsidian"

    def test_store_ignores_disallowed_theme(self, tmp_path: Path) -> None:
        # Given a theme outside the allowlist
        ts.store_last_theme("purple", str(tmp_path))
        # When the state is loaded
        assert ts.load_last_theme(str(tmp_path)) is None

    def test_store_leaves_no_temp_files(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        ts.store_last_theme("moonstone", str(tmp_path))
        # When the config directory is inspected
        leftovers = [p.name for p in (tmp_path / ".config").iterdir() if p.name.startswith(".")]
        assert leftovers == []

    def test_load_without_state_returns_none(self, tmp_path: Path) -> None:
        # Given no state file
        # When the state is loaded
        assert ts.load_last_theme(str(tmp_path)) is None

    def test_load_corrupt_state_returns_none(self, tmp_path: Path) -> None:
        # Given a state file that is not valid JSON
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text("{broken", encoding="utf-8")
        # When the state is loaded
        assert ts.load_last_theme(str(tmp_path)) is None

    def test_load_non_object_state_returns_none(self, tmp_path: Path) -> None:
        # Given a state file that is a JSON list
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text("[1, 2]", encoding="utf-8")
        # When the state is loaded
        assert ts.load_last_theme(str(tmp_path)) is None

    def test_load_disallowed_theme_returns_none(self, tmp_path: Path) -> None:
        # Given a state file whose theme is outside the allowlist
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text(json.dumps({"theme": "purple"}), encoding="utf-8")
        # When the state is loaded
        assert ts.load_last_theme(str(tmp_path)) is None

    def test_store_failure_is_swallowed(self, tmp_path: Path) -> None:
        # Given a home where the config directory cannot be created (a file in the way)
        (tmp_path / ".config").write_text("file", encoding="utf-8")
        # When a theme is stored
        ts.store_last_theme("moonstone", str(tmp_path))  # must not raise
        # Then nothing propagates to the caller and no state was read back
        assert ts.load_last_theme(str(tmp_path)) is None


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
            "obsidian",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
        )
        # Then it is written to that vault
        assert result == {
            "changed": True,
            "reloaded": True,
            "background": "#000000",
            "background_applied": False,
        }
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "obsidian"

    def test_request_persists_remembered_theme(self, tmp_path: Path) -> None:
        # Given a vault and a fresh home directory
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the theme is requested
        ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
        )
        # Then the theme is remembered for the vault watcher
        assert ts.load_last_theme(str(tmp_path)) == "moonstone"

    def test_noop_request_refreshes_remembered_theme(self, tmp_path: Path) -> None:
        # Given a vault that already has the requested theme and a stale memory
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "obsidian"}), encoding="utf-8")
        ts.store_last_theme("moonstone", str(tmp_path))
        # When the same theme is requested again
        result = ts.handle_set_theme(
            "obsidian",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
        )
        # Then the request is a no-op but the remembered theme is refreshed
        assert result == {
            "changed": False,
            "reloaded": False,
            "background": "#000000",
            "background_applied": False,
        }
        assert ts.load_last_theme(str(tmp_path)) == "obsidian"

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
        first = ts.handle_set_theme(
            "obsidian", env=env, obsidian_home=str(tmp_path), reloder=reloder
        )
        second = ts.handle_set_theme(
            "obsidian", env=env, obsidian_home=str(tmp_path), reloder=reloder
        )
        # Then the reloader runs only for the first (changed) write
        assert first == {
            "changed": True,
            "reloaded": True,
            "background": "#000000",
            "background_applied": False,
        }
        assert second == {
            "changed": False,
            "reloaded": False,
            "background": "#000000",
            "background_applied": False,
        }
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
        ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=reloder,
        )
        # Then the request environment is passed through
        assert captured["THEME_SYNC_VAULT"] == str(vault)

    def test_reload_failure_still_reports_change(self, tmp_path: Path) -> None:
        # Given a fresh vault and a reloader that fails
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the theme is applied
        result = ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: False,
        )
        # Then the change stands and the reload is reported as failed
        assert result == {
            "changed": True,
            "reloaded": False,
            "background": "#f2f4f9",
            "background_applied": False,
        }
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_reload_exception_is_contained(self, tmp_path: Path) -> None:
        # Given a fresh vault and a reloader that raises unexpectedly
        vault = tmp_path / "vault"
        vault.mkdir()

        def reloder(**kw: object) -> bool:
            raise RuntimeError("cli exploded")

        # When the theme is applied
        result = ts.handle_set_theme(
            "obsidian",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=reloder,
        )
        # Then the change stands and the error is contained
        assert result == {
            "changed": True,
            "reloaded": False,
            "background": "#000000",
            "background_applied": False,
        }
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
# registered_vaults
# --------------------------------------------------------------------------


class TestRegisteredVaults:
    def test_collects_paths_of_existing_vaults(self, tmp_path: Path) -> None:
        # Given two vaults recorded in the global config
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        data = {
            "vaults": {
                "a": {"path": str(first), "ts": 1},
                "b": {"path": str(second), "ts": 2},
            }
        }
        # When the registry is read
        result = ts.registered_vaults(data)
        # Then both existing paths are listed
        assert result == {str(first), str(second)}

    def test_skips_paths_that_do_not_exist(self, tmp_path: Path) -> None:
        # Given a recorded vault whose path is absent on disk
        data = {"vaults": {"a": {"path": str(tmp_path / "gone"), "ts": 1}}}
        # When the registry is read
        assert ts.registered_vaults(data) == set()

    def test_skips_malformed_entries(self, tmp_path: Path) -> None:
        # Given a mix of valid and invalid registry entries
        vault = tmp_path / "vault"
        vault.mkdir()
        data = {"vaults": {"a": "junk", "b": {"ts": 9}, "c": {"path": str(vault), "ts": 1}}}
        # When the registry is read
        assert ts.registered_vaults(data) == {str(vault)}

    @pytest.mark.parametrize(
        "data",
        [None, 42, "text", [1], {"vaults": "junk"}, {"vaults": None}, {}],
    )
    def test_non_dict_inputs_yield_empty_registry(self, data: object) -> None:
        # Given a global config that is not an object with a vault map
        # When the registry is read
        assert ts.registered_vaults(data) == set()


# --------------------------------------------------------------------------
# VaultWatcher (new-vault synchronization)
# --------------------------------------------------------------------------


class TestVaultWatcher:
    def test_new_vault_receives_remembered_theme(self, tmp_path: Path) -> None:
        # Given a remembered theme and a baseline registry
        ts.store_last_theme("moonstone", str(tmp_path))
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=lambda **kw: True)
        watcher.step()
        # When the user creates a vault
        vault = tmp_path / "new-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        # And the watcher steps
        changed = watcher.step()
        # Then the new vault carries the remembered theme
        assert changed == 1
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "moonstone"

    def test_existing_vaults_are_never_overwritten(self, tmp_path: Path) -> None:
        # Given a vault that existed before the watcher, with its own theme choice
        vault = tmp_path / "old"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "custom"}), encoding="utf-8")
        register_vault(tmp_path, vault)
        ts.store_last_theme("moonstone", str(tmp_path))
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=lambda **kw: True)
        # When the watcher steps with the registry unchanged
        watcher.step()
        watcher.step()
        # Then the pre-existing vault keeps its theme
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "custom"

    def test_without_remembered_theme_nothing_is_written(self, tmp_path: Path) -> None:
        # Given no remembered theme (no request was ever made)
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=lambda **kw: True)
        watcher.step()
        # When a vault appears
        vault = tmp_path / "new-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        changed = watcher.step()
        # Then no appearance file is created
        assert changed == 0
        assert not appearance(vault).exists()

    def test_unchanged_registry_is_a_noop(self, tmp_path: Path) -> None:
        # Given a remembered theme and a stable registry
        ts.store_last_theme("obsidian", str(tmp_path))
        calls: list[dict] = []

        def reloder(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=reloder)
        watcher.step()
        # When the registry stays the same
        assert watcher.step() == 0
        # Then nothing was written and the app was not reloaded
        assert calls == []

    def test_removal_then_recreation_reapplies(self, tmp_path: Path) -> None:
        # Given a vault that was synchronized once
        ts.store_last_theme("obsidian", str(tmp_path))
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=lambda **kw: True)
        watcher.step()
        vault = tmp_path / "vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        assert watcher.step() == 1
        # When the vault leaves the registry and is recreated
        write_global_config(tmp_path, {"vaults": {}})
        watcher.step()
        appearance(vault).unlink()
        register_vault(tmp_path, vault)
        # Then the remembered theme is applied again
        assert watcher.step() == 1
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "obsidian"

    def test_corrupt_global_config_is_ignored(self, tmp_path: Path) -> None:
        # Given a global config that is not valid JSON
        write_global_config(tmp_path, Ellipsis)
        ts.store_last_theme("moonstone", str(tmp_path))
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=lambda **kw: True)
        # When the watcher steps repeatedly
        assert watcher.step() == 0
        assert watcher.step() == 0
        # Then no error escapes and no vault is derived from the corrupt file

    def test_new_vault_already_at_theme_skips_reload(self, tmp_path: Path) -> None:
        # Given a new vault that already carries the remembered theme
        ts.store_last_theme("moonstone", str(tmp_path))
        calls: list[dict] = []

        def reloder(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=reloder)
        watcher.step()
        vault = tmp_path / "new-vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "moonstone"}), encoding="utf-8")
        register_vault(tmp_path, vault)
        # When the watcher steps
        assert watcher.step() == 0
        # Then the app was not reloaded
        assert calls == []

    def test_multiple_new_vaults_trigger_one_reload(self, tmp_path: Path) -> None:
        # Given a remembered theme and two vaults created at once
        ts.store_last_theme("obsidian", str(tmp_path))
        calls: list[dict] = []

        def reloder(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=reloder)
        watcher.step()
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        write_global_config(
            tmp_path,
            {"vaults": {"a": {"path": str(first), "ts": 1}, "b": {"path": str(second), "ts": 2}}},
        )
        # When the watcher steps
        assert watcher.step() == 2
        # Then both vaults were written and the app reloaded exactly once
        assert len(calls) == 1

    def test_reload_receives_environment(self, tmp_path: Path) -> None:
        # Given a watcher with a request-like environment
        captured: dict = {}

        def reloder(**kwargs: object) -> bool:
            captured.update(kwargs)
            return True

        ts.store_last_theme("moonstone", str(tmp_path))
        watcher = ts.VaultWatcher(
            home=str(tmp_path), env={"THEME_SYNC_VAULT": "/v"}, reloder=reloder
        )
        watcher.step()
        vault = tmp_path / "new-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        watcher.step()
        # Then the environment was handed to the reloader
        assert captured["env"]["THEME_SYNC_VAULT"] == "/v"

    def test_reload_exception_is_contained(self, tmp_path: Path) -> None:
        # Given a reloader that blows up
        def reloder(**kwargs: object) -> bool:
            raise RuntimeError("cli exploded")

        ts.store_last_theme("obsidian", str(tmp_path))
        watcher = ts.VaultWatcher(home=str(tmp_path), reloder=reloder)
        watcher.step()
        vault = tmp_path / "new-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        # When the watcher steps
        assert watcher.step() == 1
        # Then the write stands and nothing is raised
        assert json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"] == "obsidian"


class TestRunVaultWatcher:
    def test_loop_steps_until_stopped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a watcher that records its steps
        import threading
        import time

        steps: list[int] = []

        class FakeWatcher:
            def __init__(self, **kwargs: object) -> None:
                pass

            def step(self) -> int:
                steps.append(1)
                return 0

        monkeypatch.setattr(ts, "VaultWatcher", FakeWatcher)
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={"home": str(tmp_path), "interval": 0.01, "stop": stop},
            daemon=True,
        )
        # When the loop runs briefly and is stopped
        worker.start()
        time.sleep(0.1)
        stop.set()
        worker.join(timeout=2)
        # Then it stepped at least once and exited
        assert not worker.is_alive()
        assert len(steps) >= 1

    def test_step_failure_does_not_stop_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a watcher whose step raises
        import threading
        import time

        class ExplodingWatcher:
            def __init__(self, **kwargs: object) -> None:
                pass

            def step(self) -> int:
                raise RuntimeError("boom")

        monkeypatch.setattr(ts, "VaultWatcher", ExplodingWatcher)
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={"home": str(tmp_path), "interval": 0.01, "stop": stop},
            daemon=True,
        )
        # When the loop runs briefly and is stopped
        worker.start()
        time.sleep(0.05)
        stop.set()
        worker.join(timeout=2)
        # Then the loop survived the failures and exited
        assert not worker.is_alive()


# --------------------------------------------------------------------------
# InotifyWaiter (event-driven config change detection)
# --------------------------------------------------------------------------

INOTIFY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="inotify is Linux-only"
)


def counting_watcher(monkeypatch: pytest.MonkeyPatch, steps: list[int]) -> None:
    """Replace ts.VaultWatcher with a stub that records each step."""

    class CountingWatcher:
        def __init__(self, **kwargs: object) -> None:
            pass

        def step(self) -> int:
            steps.append(1)
            return 0

    monkeypatch.setattr(ts, "VaultWatcher", CountingWatcher)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll a predicate until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@INOTIFY
class TestInotifyWaiter:
    def test_wait_blocks_until_timeout_without_changes(self, tmp_path: Path) -> None:
        # Given a watched directory that stays untouched
        watched = tmp_path / "config"
        watched.mkdir()
        waiter = ts.InotifyWaiter(str(watched))
        # When waiting without any change
        start = time.monotonic()
        woke = waiter.wait(0.3)
        # Then it blocks until the timeout and reports no event
        assert woke is False
        assert time.monotonic() - start >= 0.25

    def test_wakes_on_file_creation(self, tmp_path: Path) -> None:
        # Given a watched directory
        watched = tmp_path / "config"
        watched.mkdir()
        waiter = ts.InotifyWaiter(str(watched))
        # When a config file appears
        (watched / "obsidian.json").write_text("{}", encoding="utf-8")
        # Then the wait reports the event
        assert waiter.wait(1.0) is True

    def test_wakes_on_atomic_replacement(self, tmp_path: Path) -> None:
        # Given a watched directory holding a config file (Electron's write
        # pattern replaces the file with a new inode rather than editing it)
        watched = tmp_path / "config"
        watched.mkdir()
        current = watched / "obsidian.json"
        current.write_text("{}", encoding="utf-8")
        waiter = ts.InotifyWaiter(str(watched))
        replacement = watched / ".obsidian.json.tmp"
        replacement.write_text('{"vaults": {}}', encoding="utf-8")
        # When the file is replaced atomically
        os.replace(str(replacement), str(current))
        # Then the wait reports the event
        assert waiter.wait(1.0) is True

    def test_wakes_on_file_deletion(self, tmp_path: Path) -> None:
        # Given a watched directory holding a config file
        watched = tmp_path / "config"
        watched.mkdir()
        current = watched / "obsidian.json"
        current.write_text("{}", encoding="utf-8")
        waiter = ts.InotifyWaiter(str(watched))
        # When the file is removed
        current.unlink()
        # Then the wait reports the event
        assert waiter.wait(1.0) is True

    def test_no_phantom_event_after_drain(self, tmp_path: Path) -> None:
        # Given a live waiter and a consumed change event
        watched = tmp_path / "config"
        watched.mkdir()
        waiter = ts.InotifyWaiter(str(watched))
        (watched / "obsidian.json").write_text("{}", encoding="utf-8")
        assert waiter.wait(1.0) is True
        # When waiting again with nothing new
        # Then the already-delivered event does not re-fire (no busy loop)
        assert waiter.wait(0.3) is False

    def test_dies_when_watched_directory_is_removed(self, tmp_path: Path) -> None:
        # Given a live waiter
        watched = tmp_path / "config"
        watched.mkdir()
        waiter = ts.InotifyWaiter(str(watched))
        # When the directory itself disappears
        watched.rmdir()
        # Then the self-removal is delivered and the waiter is unusable
        assert waiter.wait(1.0) is True
        assert waiter.is_alive is False
        assert waiter.wait(0.1) is False

    def test_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        # Given a path that does not exist
        # When constructing a waiter for it
        with pytest.raises(OSError):
            ts.InotifyWaiter(str(tmp_path / "absent"))


class TestCreateConfigWaiter:
    def test_waits_on_the_directory_holding_the_config(self, tmp_path: Path) -> None:
        # Given a home with the global config in place
        write_global_config(tmp_path, {"vaults": {}})
        # When the factory builds the waiter
        waiter = ts.create_config_waiter(str(tmp_path))
        # Then on Linux it is a live inotify waiter
        if sys.platform.startswith("linux"):
            assert isinstance(waiter, ts.InotifyWaiter)
            assert waiter.is_alive is True
        else:
            assert waiter is None

    def test_without_config_directory_returns_none(self, tmp_path: Path) -> None:
        # Given a home without the config directory (first boot, pre-Obsidian)
        # When the factory builds the waiter
        # Then it yields None so the loop falls back to polling
        assert ts.create_config_waiter(str(tmp_path)) is None


@INOTIFY
class TestRunVaultWatcherEventDriven:
    def test_applies_theme_asap_when_registry_changes(self, tmp_path: Path) -> None:
        # Given a remembered theme, an empty registry, and a live event waiter
        ts.store_last_theme("moonstone", str(tmp_path))
        config_dir = tmp_path / ".config" / "obsidian"
        config_dir.mkdir(parents=True)
        waiter = ts.InotifyWaiter(str(config_dir))
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={
                "home": str(tmp_path),
                "interval": 30.0,  # polling would take 30 s; events must not
                "stop": stop,
                "waiter": waiter,
            },
            daemon=True,
        )
        # When the user creates a vault (Obsidian writes the registry)
        worker.start()
        time.sleep(0.3)  # let the loop record the baseline first
        vault = tmp_path / "new-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        applied = wait_until(lambda: appearance(vault).exists())
        stop.set()
        worker.join(timeout=5)
        # Then the theme is applied well before the polling deadline
        assert not worker.is_alive()
        assert applied
        assert (
            json.loads(appearance(vault).read_text(encoding="utf-8"))["theme"]
            == "moonstone"
        )

    def test_applies_theme_when_registry_is_rewritten_in_place(self, tmp_path: Path) -> None:
        # Given a live event waiter and an empty registry
        ts.store_last_theme("moonstone", str(tmp_path))
        config_dir = tmp_path / ".config" / "obsidian"
        config_dir.mkdir(parents=True)
        waiter = ts.InotifyWaiter(str(config_dir))
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={
                "home": str(tmp_path),
                "interval": 30.0,  # polling would take 30 s; events must not
                "stop": stop,
                "waiter": waiter,
            },
            daemon=True,
        )
        worker.start()
        time.sleep(0.3)  # let the loop record the baseline first
        # When Obsidian registers a vault, then rewrites the same file in
        # place (fs.writeFileSync, no new inode) to register another
        first = tmp_path / "first-vault"
        first.mkdir()
        register_vault(tmp_path, first, index="one")
        applied_first = wait_until(lambda: appearance(first).exists())
        second = tmp_path / "second-vault"
        second.mkdir()
        write_global_config(
            tmp_path,
            {
                "vaults": {
                    "one": {"path": str(first), "ts": 1},
                    "two": {"path": str(second), "ts": 2},
                }
            },
        )
        applied_second = wait_until(lambda: appearance(second).exists())
        stop.set()
        worker.join(timeout=5)
        # Then both writes were picked up by events, not polling
        assert not worker.is_alive()
        assert applied_first
        assert applied_second

    def test_idle_loop_does_not_spin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a counting watcher, a live event waiter, and no config changes
        steps: list[int] = []
        counting_watcher(monkeypatch, steps)
        config_dir = tmp_path / ".config" / "obsidian"
        config_dir.mkdir(parents=True)
        waiter = ts.InotifyWaiter(str(config_dir))
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={
                "home": str(tmp_path),
                "interval": 0.01,  # a polling loop would step ~50 times
                "stop": stop,
                "waiter": waiter,
            },
            daemon=True,
        )
        # When the loop runs idle for a while
        worker.start()
        time.sleep(0.6)
        stop.set()
        worker.join(timeout=5)
        # Then it stepped only for the initial baseline, not on a timer
        assert not worker.is_alive()
        assert len(steps) <= 2

    def test_falls_back_to_polling_when_inotify_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a platform where the factory yields no waiter
        steps: list[int] = []
        counting_watcher(monkeypatch, steps)
        monkeypatch.setattr(ts, "create_config_waiter", lambda home: None)
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={"home": str(tmp_path), "interval": 0.01, "stop": stop},
            daemon=True,
        )
        # When the loop runs briefly
        worker.start()
        time.sleep(0.3)
        stop.set()
        worker.join(timeout=5)
        # Then it keeps stepping on the polling cadence and exits cleanly
        assert not worker.is_alive()
        assert len(steps) >= 3

    def test_recovers_to_polling_when_the_waiter_dies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given an injected waiter that is already dead (dir deleted, watch gone)
        steps: list[int] = []
        counting_watcher(monkeypatch, steps)
        monkeypatch.setattr(ts, "create_config_waiter", lambda home: None)

        class DeadWaiter:
            is_alive = False

            def wait(self, timeout: float) -> bool:
                return False

        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={
                "home": str(tmp_path),
                "interval": 0.01,
                "stop": stop,
                "waiter": DeadWaiter(),
            },
            daemon=True,
        )
        # When the loop runs briefly
        worker.start()
        time.sleep(0.3)
        stop.set()
        worker.join(timeout=5)
        # Then polling takes over and the loop exits cleanly
        assert not worker.is_alive()
        assert len(steps) >= 3

    def test_directory_created_later_is_synced(self, tmp_path: Path) -> None:
        # Given a home without the config directory (first boot) and a
        # remembered theme
        ts.store_last_theme("moonstone", str(tmp_path))
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={"home": str(tmp_path), "interval": 0.05, "stop": stop},
            daemon=True,
        )
        # When Obsidian later creates the config and registers a vault
        worker.start()
        time.sleep(0.2)
        vault = tmp_path / "late-vault"
        vault.mkdir()
        register_vault(tmp_path, vault)
        applied = wait_until(lambda: appearance(vault).exists())
        stop.set()
        worker.join(timeout=5)
        # Then the late-registered vault still receives the theme
        assert not worker.is_alive()
        assert applied

    def test_stop_is_honored_during_event_wait(self, tmp_path: Path) -> None:
        # Given a live event waiter with no events arriving
        config_dir = tmp_path / ".config" / "obsidian"
        config_dir.mkdir(parents=True)
        waiter = ts.InotifyWaiter(str(config_dir))
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_vault_watcher,
            kwargs={
                "home": str(tmp_path),
                "interval": 30.0,
                "stop": stop,
                "waiter": waiter,
            },
            daemon=True,
        )
        # When the loop is stopped while blocked in the event wait
        worker.start()
        time.sleep(0.1)
        stop.set()
        worker.join(timeout=3)
        # Then it exits promptly (the wait is sliced, not unbounded)
        assert not worker.is_alive()


# --------------------------------------------------------------------------
# init-obsidian-cli oneshot (s6-rc service)
# --------------------------------------------------------------------------

ONESHOT_RUN = (
    Path(__file__).resolve().parent.parent
    / "obsidian"
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
    @pytest.fixture(autouse=True)
    def obsidian_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Keep the persisted last-theme state inside the test sandbox
        monkeypatch.setenv("OBSIDIAN_HOME", str(tmp_path))

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


# --------------------------------------------------------------------------
# background color normalization
# --------------------------------------------------------------------------


class TestFormatHex:
    def test_hex_passthrough(self) -> None:
        # Given a parsed opaque hex color
        # When normalized
        assert ts.format_hex(ts.parse_color("#112233")) == "#112233"

    def test_uppercase_normalizes_to_lowercase(self) -> None:
        # Given an uppercase hex color
        # When normalized
        assert ts.format_hex(ts.parse_color("#ABCDEF")) == "#abcdef"

    def test_short_hex_expands(self) -> None:
        # Given a 3-digit hex color
        # When normalized
        assert ts.format_hex(ts.parse_color("#abc")) == "#aabbcc"

    def test_alpha_is_ignored(self) -> None:
        # Given a translucent color
        # When normalized the result is opaque
        assert ts.format_hex(ts.parse_color("#11223344")) == "#112233"
        assert ts.format_hex(ts.parse_color("rgba(1, 2, 3, 0.2)")) == "#010203"

    def test_float_channels_round(self) -> None:
        # Given float channels from an rgb() value
        # When normalized each channel is rounded to an integer
        assert ts.format_hex(ts.parse_color("rgba(10.4, 20.6, 30.2, 1)")) == "#0a151e"

    def test_max_channels(self) -> None:
        # Given full-brightness channels
        # When normalized
        assert ts.format_hex(ts.parse_color("rgb(255, 255, 255)")) == "#ffffff"

    def test_zero_channels(self) -> None:
        # Given a black color
        # When normalized
        assert ts.format_hex(ts.parse_color("#000000")) == "#000000"


class TestNormalizeRequestedBackground:
    def test_missing_color_uses_dark_default(self) -> None:
        # Given a dark theme and no color
        # When resolved
        assert ts.normalize_requested_background("obsidian", None) == "#000000"

    def test_missing_color_uses_light_default(self) -> None:
        # Given a light theme and no color
        # When resolved
        assert ts.normalize_requested_background("moonstone", None) == "#f2f4f9"

    def test_matching_dark_color(self) -> None:
        # Given the dark theme and a dark color
        # When resolved
        assert ts.normalize_requested_background("obsidian", "#202225") == "#202225"

    def test_matching_light_color(self) -> None:
        # Given the light theme and a light color
        # When resolved
        assert ts.normalize_requested_background("moonstone", "rgba(242, 244, 249, 1)") == "#f2f4f9"

    def test_light_color_with_dark_theme_rejected(self) -> None:
        # Given the dark theme and a light color
        # When resolved
        with pytest.raises(ValueError, match="does not match theme"):
            ts.normalize_requested_background("obsidian", "#ffffff")

    def test_dark_color_with_light_theme_rejected(self) -> None:
        # Given the light theme and a dark color
        # When resolved
        with pytest.raises(ValueError, match="does not match theme"):
            ts.normalize_requested_background("moonstone", "#111111")

    def test_unparseable_color_rejected(self) -> None:
        # Given a color value that is not a CSS color
        # When resolved
        with pytest.raises(ValueError, match="invalid color"):
            ts.normalize_requested_background("obsidian", "hsl(0, 0%, 50%)")

    @pytest.mark.parametrize("value", ["", "   ", "not-a-color", "#12", "#12345", "#GGGGGG"])
    def test_malformed_values_rejected(self, value: object) -> None:
        # Given a malformed color value
        # When resolved for either theme
        with pytest.raises(ValueError):
            ts.normalize_requested_background("obsidian", value)  # type: ignore[arg-type]

    def test_boundary_luminance_follows_the_theme_classifier(self) -> None:
        # Given the two grays on either side of the 0.5 luminance boundary
        # When resolved against their matching themes
        assert ts.normalize_requested_background("obsidian", "#bbbbbb") == "#bbbbbb"
        assert ts.normalize_requested_background("moonstone", "#bcbcbc") == "#bcbcbc"


# --------------------------------------------------------------------------
# remembered background state
# --------------------------------------------------------------------------


class TestLastBackgroundState:
    def test_store_and_load_roundtrip(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        # When a theme with a background is stored
        ts.store_last_theme("moonstone", str(tmp_path), background="#f2f4f9")
        # Then the normalized color is read back
        assert ts.load_last_background(str(tmp_path)) == "#f2f4f9"

    def test_load_normalizes_the_stored_value(self, tmp_path: Path) -> None:
        # Given a stored background written by an older or foreign client
        ts.store_last_theme("obsidian", str(tmp_path), background="#0B0C10")
        # When loaded
        assert ts.load_last_background(str(tmp_path)) == "#0b0c10"

    def test_load_without_state_returns_none(self, tmp_path: Path) -> None:
        # Given no state file
        # When loaded
        assert ts.load_last_background(str(tmp_path)) is None

    def test_load_corrupt_state_returns_none(self, tmp_path: Path) -> None:
        # Given a state file that is not valid JSON
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text("{broken", encoding="utf-8")
        # When loaded
        assert ts.load_last_background(str(tmp_path)) is None

    def test_load_non_string_background_returns_none(self, tmp_path: Path) -> None:
        # Given a state file whose background is a number
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text(json.dumps({"theme": "obsidian", "background": 42}), encoding="utf-8")
        # When loaded
        assert ts.load_last_background(str(tmp_path)) is None

    def test_load_unparseable_background_returns_none(self, tmp_path: Path) -> None:
        # Given a state file whose background is not a color
        state_file(tmp_path).parent.mkdir(parents=True)
        state_file(tmp_path).write_text(json.dumps({"theme": "obsidian", "background": "garbage"}), encoding="utf-8")
        # When loaded
        assert ts.load_last_background(str(tmp_path)) is None

    def test_theme_only_store_preserves_existing_background(self, tmp_path: Path) -> None:
        # Given a stored theme with a remembered background
        ts.store_last_theme("moonstone", str(tmp_path), background="#f2f4f9")
        # When another theme is stored without a background
        ts.store_last_theme("obsidian", str(tmp_path))
        # Then the background survives and the new theme is remembered
        assert ts.load_last_theme(str(tmp_path)) == "obsidian"
        assert ts.load_last_background(str(tmp_path)) == "#f2f4f9"

    def test_theme_only_store_adds_no_background_key_when_absent(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        ts.store_last_theme("obsidian", str(tmp_path))
        # When the state file is read
        data = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        # Then it carries only the theme
        assert data == {"theme": "obsidian"}

    def test_unparseable_background_argument_is_dropped(self, tmp_path: Path) -> None:
        # Given a fresh home directory
        ts.store_last_theme("moonstone", str(tmp_path), background="garbage")
        # When the state file is read
        data = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        # Then only the theme is stored
        assert data == {"theme": "moonstone"}

    def test_disallowed_theme_stores_nothing(self, tmp_path: Path) -> None:
        # Given a theme outside the allowlist
        ts.store_last_theme("purple", str(tmp_path), background="#111111")
        # When the state file is inspected
        assert not state_file(tmp_path).exists()

    def test_store_failure_is_swallowed(self, tmp_path: Path) -> None:
        # Given a home where the config directory cannot be created (a file in the way)
        (tmp_path / ".config").write_text("file", encoding="utf-8")
        # When a background is stored
        ts.store_last_theme("moonstone", str(tmp_path), background="#f2f4f9")  # must not raise
        # Then nothing propagates and nothing was read back
        assert ts.load_last_background(str(tmp_path)) is None


# --------------------------------------------------------------------------
# Wayland display discovery
# --------------------------------------------------------------------------


def touch_lock(runtime_dir: Path, name: str) -> int:
    """Create a wayland lock file and return its inode."""
    path = runtime_dir / name
    path.write_text("", encoding="ascii")
    return path.stat().st_ino


class TestFindLabwcDisplay:
    def test_picks_the_labwc_owned_display(self, tmp_path: Path) -> None:
        # Given a runtime directory where Selkies owns wayland-1 and labwc
        # wayland-0 (the nested topology of the base image)
        selkies_ino = touch_lock(tmp_path, "wayland-1.lock")
        labwc_ino = touch_lock(tmp_path, "wayland-0.lock")
        proc_map = {selkies_ino: "selkies", labwc_ino: "labwc"}
        # When the display is discovered
        # Then labwc's display is named, never the streamed root
        assert ts.find_labwc_display(str(tmp_path), proc_map) == "wayland-0"

    def test_no_lock_files_returns_none(self, tmp_path: Path) -> None:
        # Given an empty runtime directory
        (tmp_path / "other").write_text("", encoding="ascii")
        # When the display is discovered
        assert ts.find_labwc_display(str(tmp_path), {}) is None

    def test_only_non_labwc_owners_returns_none(self, tmp_path: Path) -> None:
        # Given locks held by processes that are not labwc
        selkies_ino = touch_lock(tmp_path, "wayland-1.lock")
        # When the display is discovered
        assert ts.find_labwc_display(str(tmp_path), {selkies_ino: "selkies"}) is None

    def test_broken_lock_symlink_is_skipped(self, tmp_path: Path) -> None:
        # Given an unstatable lock file (a broken symlink)
        os.symlink(str(tmp_path / "gone"), str(tmp_path / "wayland-9.lock"))
        good_ino = touch_lock(tmp_path, "wayland-0.lock")
        # When the display is discovered the broken entry is skipped
        assert ts.find_labwc_display(str(tmp_path), {good_ino: "labwc"}) == "wayland-0"

    def test_multiple_labwc_locks_resolve_to_the_lowest(self, tmp_path: Path) -> None:
        # Given two labwc-owned displays
        first = touch_lock(tmp_path, "wayland-2.lock")
        second = touch_lock(tmp_path, "wayland-0.lock")
        proc_map = {first: "labwc", second: "labwc"}
        # When the display is discovered
        assert ts.find_labwc_display(str(tmp_path), proc_map) == "wayland-0"

    def test_missing_runtime_directory_returns_none(self, tmp_path: Path) -> None:
        # Given a runtime directory that does not exist
        # When the display is discovered
        assert ts.find_labwc_display(str(tmp_path / "absent"), {}) is None

    def test_lock_owner_is_found_through_the_scan(self, tmp_path: Path) -> None:
        # Given the live topology: Selkies holds wayland-1.lock open as a
        # regular file, labwc holds wayland-0.lock (both regular files —
        # lock files are not sockets)
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        selkies_lock = runtime / "wayland-1.lock"
        labwc_lock = runtime / "wayland-0.lock"
        selkies_lock.write_text("", encoding="ascii")
        labwc_lock.write_text("", encoding="ascii")
        proc_dir = tmp_path / "proc"
        fake_process(proc_dir, 330, "selkies", {14: str(selkies_lock)})
        fake_process(proc_dir, 393, "labwc", {18: str(labwc_lock)})
        # When the scan and the discovery are composed
        mapping = ts.scan_process_comms(str(proc_dir))
        # Then labwc's display is named, never the streamed root
        assert ts.find_labwc_display(str(runtime), mapping) == "wayland-0"


def fake_process(proc_dir: Path, pid: int, comm: str, fd_links: dict[int, str]) -> None:
    """Lay down /proc/<pid> with a comm file and the given fd symlinks."""
    pid_dir = proc_dir / str(pid)
    fd_dir = pid_dir / "fd"
    fd_dir.mkdir(parents=True)
    (pid_dir / "comm").write_text(comm + "\n", encoding="ascii")
    for number, target in fd_links.items():
        os.symlink(target, str(fd_dir / str(number)))


class TestScanProcessComms:
    def test_returns_string_map(self) -> None:
        # Given the real /proc of the test host
        # When scanned
        result = ts.scan_process_comms()
        # Then every value is a process name
        assert isinstance(result, dict)
        assert all(isinstance(comm, str) for comm in result.values())

    def test_missing_proc_directory_is_empty(self, tmp_path: Path) -> None:
        # Given a proc directory that does not exist
        # When scanned
        assert ts.scan_process_comms(str(tmp_path / "absent")) == {}

    def test_socket_fds_are_mapped(self, tmp_path: Path) -> None:
        # Given a process with a socket fd
        proc_dir = tmp_path / "proc"
        fake_process(proc_dir, 4242, "labwc", {3: "socket:[123456789]"})
        # When scanned
        mapping = ts.scan_process_comms(str(proc_dir))
        # Then the socket inode names the holder
        assert mapping.get(123456789) == "labwc"

    def test_regular_file_fds_are_mapped(self, tmp_path: Path) -> None:
        # Given a process holding a regular file open (a Wayland lock file
        # is kept open by its server exactly like this)
        proc_dir = tmp_path / "proc"
        lock = tmp_path / "wayland-0.lock"
        lock.write_text("", encoding="ascii")
        fake_process(proc_dir, 4242, "labwc", {18: str(lock)})
        # When scanned
        mapping = ts.scan_process_comms(str(proc_dir))
        # Then the file's inode names the holder
        assert mapping.get(lock.stat().st_ino) == "labwc"

    def test_unstatable_targets_are_skipped(self, tmp_path: Path) -> None:
        # Given a process with a fd whose target no longer exists
        proc_dir = tmp_path / "proc"
        fake_process(proc_dir, 4242, "labwc", {7: str(tmp_path / "deleted")})
        # When scanned
        mapping = ts.scan_process_comms(str(proc_dir))
        # Then nothing is recorded for it
        assert mapping == {}

    def test_first_holder_wins(self, tmp_path: Path) -> None:
        # Given two processes holding the same file
        proc_dir = tmp_path / "proc"
        shared = tmp_path / "shared"
        shared.write_text("", encoding="ascii")
        fake_process(proc_dir, 100, "selkies", {2: str(shared)})
        fake_process(proc_dir, 200, "labwc", {3: str(shared)})
        # When scanned
        mapping = ts.scan_process_comms(str(proc_dir))
        # Then exactly one holder is recorded
        assert mapping.get(shared.stat().st_ino) in ("selkies", "labwc")


# --------------------------------------------------------------------------
# DesktopBackground manager
# --------------------------------------------------------------------------


class FakePopen:
    """Records spawned processes; poll() stays None until the test kills it."""

    def __init__(self, argv: list, env: dict | None = None, **kwargs: object) -> None:
        self.argv = list(argv)
        self.env = dict(env or {})
        self.returncode: int | None = None
        self.terminated = False
        FakePopen.instances.append(self)

    instances: list = []

    def poll(self) -> int | None:
        return self.returncode

    def die(self) -> None:
        self.returncode = 101

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.returncode is None else self.returncode

    def kill(self) -> None:
        self.returncode = 0


def wayland_manager(
    tmp_path: Path,
    proc_scan: Callable[[], dict[int, str]],
    display: str | None = None,
    reapply_interval: float | None = None,
) -> ts.DesktopBackground:
    runtime = tmp_path / ".XDG"
    runtime.mkdir(parents=True, exist_ok=True)
    return ts.DesktopBackground(
        env={
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(runtime),
            "PIXELFLUX_WAYLAND": "true",
        },
        display=display,
        reapply_interval=reapply_interval,
        proc_scan=proc_scan,
    )


def x_manager(
    tmp_path: Path,
    display: str = ":9",
    reapply_interval: float = 30.0,
) -> ts.DesktopBackground:
    return ts.DesktopBackground(
        env={"HOME": str(tmp_path), "PIXELFLUX_WAYLAND": "false", "DISPLAY": display},
        reapply_interval=reapply_interval,
    )


def labwc_lock(runtime: Path) -> int:
    return touch_lock(runtime, "wayland-0.lock")


class TestDesktopBackgroundWayland:
    @pytest.fixture(autouse=True)
    def fake_popen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakePopen.instances = []
        monkeypatch.setattr(ts.subprocess, "Popen", FakePopen)

    def test_spawns_swaybg_on_the_labwc_display(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a nested topology: Selkies on wayland-1, labwc on wayland-0
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        selkies_ino = touch_lock(runtime, "wayland-1.lock")
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(
            tmp_path, proc_scan=lambda: {selkies_ino: "selkies", labwc_ino: "labwc"}
        )
        # When a background is requested
        started = manager.set_color("#112233")
        # Then swaybg is started against labwc's display, never the root
        assert started is True
        assert len(FakePopen.instances) == 1
        process = FakePopen.instances[0]
        assert process.argv == ["/usr/bin/swaybg", "-c", "#112233"]
        assert process.env["WAYLAND_DISPLAY"] == "wayland-0"
        assert process.env["XDG_RUNTIME_DIR"] == str(runtime)
        assert process.env["HOME"] == str(tmp_path)

    def test_missing_labwc_defers_the_start_until_it_appears(
        self, tmp_path: Path
    ) -> None:
        # Given a runtime directory whose only lock is Selkies'
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        selkies_ino = touch_lock(runtime, "wayland-1.lock")
        scan_state = {selkies_ino: "selkies"}
        manager = wayland_manager(tmp_path, proc_scan=lambda: dict(scan_state))
        # When a background is requested before the desktop is up
        assert manager.set_color("#112233") is False
        assert FakePopen.instances == []
        # And labwc starts, taking wayland-0
        labwc_ino = labwc_lock(runtime)
        scan_state[labwc_ino] = "labwc"
        # When the supervisor steps
        assert manager.step() is True
        # Then the background is finally running
        assert len(FakePopen.instances) == 1
        assert FakePopen.instances[0].env["WAYLAND_DISPLAY"] == "wayland-0"

    def test_same_color_is_a_noop(self, tmp_path: Path) -> None:
        # Given a manager whose desktop is up
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        # When the same color is requested twice
        assert manager.set_color("#112233") is True
        assert manager.set_color("#112233") is False
        # Then only one process was started
        assert len(FakePopen.instances) == 1

    def test_color_switch_replaces_the_process(self, tmp_path: Path) -> None:
        # Given a running background
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        manager.set_color("#112233")
        # When a different color is requested
        assert manager.set_color("#332211") is True
        # Then the old process is terminated and the new one carries the color
        old, new = FakePopen.instances
        assert old.terminated is True
        assert new.argv[-1] == "#332211"
        assert len(FakePopen.instances) == 2

    def test_dead_process_is_restarted_by_a_step(self, tmp_path: Path) -> None:
        # Given a running background that then exits
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        manager.set_color("#112233")
        FakePopen.instances[0].die()
        # When the supervisor steps
        assert manager.step() is True
        # Then a fresh process is running
        assert len(FakePopen.instances) == 2
        assert FakePopen.instances[1].poll() is None

    def test_alive_process_is_not_restarted_by_a_step(self, tmp_path: Path) -> None:
        # Given a healthy background
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        manager.set_color("#112233")
        # When the supervisor steps
        assert manager.step() is True
        # Then nothing new was spawned
        assert len(FakePopen.instances) == 1

    def test_missing_binary_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a swaybg binary that cannot be executed
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})

        def broken(argv: list, **kwargs: object) -> object:
            raise FileNotFoundError("swaybg")

        monkeypatch.setattr(ts.subprocess, "Popen", broken)
        # When a background is requested
        assert manager.set_color("#112233") is False
        # Then no process exists and the target is remembered for later
        assert FakePopen.instances == []
        assert manager.target == "#112233"

    def test_explicit_display_bypasses_discovery(self, tmp_path: Path) -> None:
        # Given a manager with an explicitly configured display and no locks
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        manager = wayland_manager(tmp_path, proc_scan=lambda: {}, display="wayland-2")
        # When a background is requested
        assert manager.set_color("#112233") is True
        # Then the configured display is used
        assert FakePopen.instances[0].env["WAYLAND_DISPLAY"] == "wayland-2"

    def test_stop_kills_the_process_and_forgets_the_target(self, tmp_path: Path) -> None:
        # Given a running background
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        manager.set_color("#112233")
        # When the manager is stopped
        manager.stop()
        # Then the process is gone, the target is forgotten, and steps no-op
        assert FakePopen.instances[0].terminated is True
        assert manager.target is None
        assert manager.step() is False
        assert len(FakePopen.instances) == 1


class TestDesktopBackgroundXMode:
    @pytest.fixture(autouse=True)
    def fake_run(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Record xsetroot calls; returncode is driven by the test."""

        state = {"returncode": 0, "calls": []}

        def fake_run(argv: list, **kwargs: object):
            state["calls"].append({"argv": list(argv), **kwargs})

            class Result:
                returncode = state["returncode"]

            return Result()

        monkeypatch.setattr(ts.subprocess, "run", fake_run)
        return state

    def test_applies_the_root_color(self, tmp_path: Path, fake_run: dict) -> None:
        # Given an X-mode manager (Xvfb on :9)
        manager = x_manager(tmp_path, display=":9")
        # When a background is requested
        applied = manager.set_color("#f2f4f9")
        # Then xsetroot is invoked for that display with an opaque color
        assert applied is True
        call = fake_run["calls"][0]
        assert call["argv"] == ["/usr/bin/xsetroot", "-solid", "#f2f4f9"]
        assert call["env"]["DISPLAY"] == ":9"

    def test_display_defaults_to_colon_one(self, tmp_path: Path, fake_run: dict) -> None:
        # Given an X-mode manager with no DISPLAY in the environment
        manager = ts.DesktopBackground(env={"HOME": str(tmp_path)})
        # When a background is requested
        manager.set_color("#000000")
        # Then the image's X display is used
        assert fake_run["calls"][0]["env"]["DISPLAY"] == ":1"

    def test_down_x_server_is_retried_until_it_appears(self, tmp_path: Path, fake_run: dict) -> None:
        # Given an X server that is not up yet
        manager = x_manager(tmp_path)
        fake_run["returncode"] = 1
        # When a background is requested
        assert manager.set_color("#f2f4f9") is False
        assert manager.step() is False
        # And the X server comes up
        fake_run["returncode"] = 0
        # When the supervisor steps
        assert manager.step() is True
        # Then the color was applied exactly once
        assert len(fake_run["calls"]) == 3

    def test_reapply_cadence(self, tmp_path: Path, fake_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given a short reapply interval and a healthy X server
        manager = x_manager(tmp_path, reapply_interval=0.05)
        monkeypatch.setattr(ts.time, "monotonic", lambda: 100.0)
        assert manager.set_color("#f2f4f9") is True
        # When a step runs inside the cadence
        monkeypatch.setattr(ts.time, "monotonic", lambda: 100.01)
        assert manager.step() is True
        assert len(fake_run["calls"]) == 1
        # And a step runs after the cadence elapsed
        monkeypatch.setattr(ts.time, "monotonic", lambda: 100.06)
        assert manager.step() is True
        # Then the color was re-applied exactly once
        assert len(fake_run["calls"]) == 2

    def test_same_color_is_a_noop(self, tmp_path: Path, fake_run: dict) -> None:
        # Given a manager whose color is already in effect
        manager = x_manager(tmp_path, reapply_interval=60.0)
        manager.set_color("#f2f4f9")
        # When the same color is requested again
        assert manager.set_color("#f2f4f9") is False
        # Then xsetroot was not invoked a second time
        assert len(fake_run["calls"]) == 1

    def test_missing_binary_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Given an xsetroot binary that cannot be executed
        manager = x_manager(tmp_path)

        def broken(argv: list, **kwargs: object):
            raise FileNotFoundError("xsetroot")

        monkeypatch.setattr(ts.subprocess, "run", broken)
        # When a background is requested
        assert manager.set_color("#f2f4f9") is False
        # Then the target is remembered for the supervisor
        assert manager.target == "#f2f4f9"

    def test_stop_resets_state(self, tmp_path: Path, fake_run: dict) -> None:
        # Given an applied background
        manager = x_manager(tmp_path)
        manager.set_color("#f2f4f9")
        # When the manager is stopped
        manager.stop()
        # Then the target is forgotten and steps no-op
        assert manager.target is None
        assert manager.step() is False
        assert len(fake_run["calls"]) == 1


class TestRunBackgroundSupervisor:
    @pytest.fixture(autouse=True)
    def fake_popen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakePopen.instances = []
        monkeypatch.setattr(ts.subprocess, "Popen", FakePopen)

    def test_restarts_a_dead_background(self, tmp_path: Path) -> None:
        # Given a supervisor over a wayland manager
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        labwc_ino = labwc_lock(runtime)
        manager = wayland_manager(tmp_path, proc_scan=lambda: {labwc_ino: "labwc"})
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_background_supervisor,
            kwargs={"manager": manager, "stop": stop, "interval": 0.02},
            daemon=True,
        )
        manager.set_color("#112233")
        # When the loop runs and the background dies
        worker.start()
        FakePopen.instances[0].die()
        restarted = wait_until(lambda: len(FakePopen.instances) >= 2, timeout=3)
        stop.set()
        worker.join(timeout=3)
        # Then it was restarted and the loop exited
        assert not worker.is_alive()
        assert restarted

    def test_retries_until_the_compositor_is_up(self, tmp_path: Path) -> None:
        # Given a supervisor, a target color, and no desktop yet
        runtime = tmp_path / ".XDG"
        runtime.mkdir()
        scan_state: dict[int, str] = {}
        manager = wayland_manager(tmp_path, proc_scan=lambda: dict(scan_state))
        manager.set_color("#112233")  # deferred: no labwc display exists yet
        assert FakePopen.instances == []
        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_background_supervisor,
            kwargs={"manager": manager, "stop": stop, "interval": 0.02},
            daemon=True,
        )
        worker.start()
        time.sleep(0.1)
        assert FakePopen.instances == []  # still nothing to attach to
        # And labwc comes up
        labwc_ino = labwc_lock(runtime)
        scan_state[labwc_ino] = "labwc"
        started = wait_until(lambda: len(FakePopen.instances) >= 1, timeout=3)
        stop.set()
        worker.join(timeout=3)
        # Then the supervisor attached once labwc existed
        assert not worker.is_alive()
        assert started
        assert FakePopen.instances[0].env["WAYLAND_DISPLAY"] == "wayland-0"

    def test_step_failure_does_not_stop_the_loop(self, tmp_path: Path) -> None:
        # Given a supervisor over a manager whose step always raises
        class ExplodingManager(ts.DesktopBackground):
            def step(self) -> bool:
                raise RuntimeError("boom")

        stop = threading.Event()
        worker = threading.Thread(
            target=ts.run_background_supervisor,
            kwargs={
                "manager": ExplodingManager(env={"HOME": str(tmp_path)}),
                "stop": stop,
                "interval": 0.02,
            },
            daemon=True,
        )
        # When the loop runs long enough to fail several times
        worker.start()
        time.sleep(0.15)
        assert worker.is_alive()
        # Then a stop request still exits the loop cleanly
        stop.set()
        worker.join(timeout=3)
        assert not worker.is_alive()


# --------------------------------------------------------------------------
# handle_set_theme with a background color
# --------------------------------------------------------------------------


class FakeBackgroundManager:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def set_color(self, color: str) -> bool:
        self.applied.append(color)
        return True


class ExplodingBackgroundManager:
    def set_color(self, color: str) -> bool:
        raise RuntimeError("boom")


class TestHandleSetThemeColor:
    @pytest.fixture(autouse=True)
    def clean_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ts, "_background_manager", None)

    def test_matching_color_is_stored_and_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a fresh vault and a background manager
        vault = tmp_path / "vault"
        vault.mkdir()
        manager = FakeBackgroundManager()
        monkeypatch.setattr(ts, "_background_manager", manager)
        # When a theme is requested together with its exact background color
        result = ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
            color="rgba(242, 244, 249, 1)",
        )
        # Then the normalized color is reported, applied, and remembered
        assert result["background"] == "#f2f4f9"
        assert result["background_applied"] is True
        assert manager.applied == ["#f2f4f9"]
        assert ts.load_last_background(str(tmp_path)) == "#f2f4f9"

    def test_missing_color_uses_the_theme_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a fresh vault and a background manager
        vault = tmp_path / "vault"
        vault.mkdir()
        manager = FakeBackgroundManager()
        monkeypatch.setattr(ts, "_background_manager", manager)
        # When a theme is requested without a color
        result = ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
        )
        # Then the per-theme default is applied and remembered
        assert result["background"] == "#f2f4f9"
        assert result["background_applied"] is True
        assert ts.load_last_background(str(tmp_path)) == "#f2f4f9"

    def test_mismatching_color_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a fresh vault and a light color paired with the dark theme
        vault = tmp_path / "vault"
        vault.mkdir()
        manager = FakeBackgroundManager()
        monkeypatch.setattr(ts, "_background_manager", manager)
        # When the request is handled
        with pytest.raises(ValueError, match="does not match theme"):
            ts.handle_set_theme(
                "obsidian",
                env={"THEME_SYNC_VAULT": str(vault)},
                obsidian_home=str(tmp_path),
                color="#ffffff",
            )
        # Then nothing is written, painted, or remembered
        assert not appearance(vault).exists()
        assert manager.applied == []
        assert ts.load_last_background(str(tmp_path)) is None

    def test_invalid_color_rejected(self, tmp_path: Path) -> None:
        # Given a fresh vault and a value that is not a CSS color
        vault = tmp_path / "vault"
        vault.mkdir()
        # When the request is handled
        with pytest.raises(ValueError, match="invalid color"):
            ts.handle_set_theme(
                "moonstone",
                env={"THEME_SYNC_VAULT": str(vault)},
                obsidian_home=str(tmp_path),
                color="hsl(0, 0%, 50%)",
            )
        # Then nothing is written
        assert not appearance(vault).exists()

    def test_theme_unchanged_color_change_still_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a vault already on the requested theme and a background manager
        vault = tmp_path / "vault"
        appearance(vault).parent.mkdir(parents=True)
        appearance(vault).write_text(json.dumps({"theme": "moonstone"}), encoding="utf-8")
        manager = FakeBackgroundManager()
        monkeypatch.setattr(ts, "_background_manager", manager)
        # When the same theme is requested with a new light color
        result = ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
            color="#ffffff",
        )
        # Then the write is a no-op but the background is still updated
        assert result["changed"] is False
        assert result["reloaded"] is False
        assert result["background"] == "#ffffff"
        assert result["background_applied"] is True
        assert manager.applied == ["#ffffff"]

    def test_manager_failure_reports_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a fresh vault and a background manager that fails
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(ts, "_background_manager", ExplodingBackgroundManager())
        # When the theme is requested
        result = ts.handle_set_theme(
            "moonstone",
            env={"THEME_SYNC_VAULT": str(vault)},
            obsidian_home=str(tmp_path),
            reloder=lambda **kw: True,
            color="#f2f4f9",
        )
        # Then the theme stands and the failed paint is reported, not raised
        assert result["changed"] is True
        assert result["reloaded"] is True
        assert result["background_applied"] is False


# --------------------------------------------------------------------------
# HTTP interface: background color parameter
# --------------------------------------------------------------------------


class TestHttpApiBackground:
    @pytest.fixture(autouse=True)
    def obsidian_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Keep the persisted state in the sandbox and no background manager installed
        monkeypatch.setenv("OBSIDIAN_HOME", str(tmp_path))
        monkeypatch.setattr(ts, "_background_manager", None)

    def test_color_param_is_reported(
        self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a vault and a working CLI
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: True)
        # When the API is called with a theme and its exact background color
        status, payload = post(server, "/api/set-theme?theme=moonstone&color=%23f2f4f9")
        # Then it succeeds and reports the normalized color
        assert status == 200
        assert payload["status"] == "ok"
        assert payload["background"] == "#f2f4f9"
        assert payload["background_applied"] is False

    def test_missing_color_reports_the_theme_default(
        self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a vault and a working CLI
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        monkeypatch.setattr(ts, "reload_obsidian", lambda env: True)
        # When the API is called with only a theme
        status, payload = post(server, "/api/set-theme?theme=obsidian")
        # Then the dark theme default is reported
        assert status == 200
        assert payload["background"] == "#000000"

    def test_mismatching_color_is_rejected(
        self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a vault
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        # When the API is called with a light color for the dark theme
        status, payload = post(server, "/api/set-theme?theme=obsidian&color=%23ffffff")
        # Then it is a contract violation and nothing was painted
        assert status == 400
        assert payload["status"] == "error"

    def test_invalid_color_is_rejected(
        self, server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a vault
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("THEME_SYNC_VAULT", str(vault))
        # When the API is called with an unparseable color
        status, payload = post(server, "/api/set-theme?theme=moonstone&color=%23zz")
        # Then the request is rejected
        assert status == 400
        assert payload["status"] == "error"
