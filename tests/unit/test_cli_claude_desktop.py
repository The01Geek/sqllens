# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `sqllens claude-desktop install`.

Tests target the installer module directly (pure-function path) plus the CLI
glue (via Typer's CliRunner). No real Claude Desktop, network, or DB access.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest
from typer.testing import CliRunner

from sqllens.cli import app
from sqllens.installers.claude_desktop import (
    InstallError,
    InstallOptions,
    default_config_path,
    default_memory_dir,
    default_working_dir,
    derive_default_name,
    generate_cmd_launcher,
    generate_toml,
    make_backup_path,
    merge_into_mcp_servers,
    resolve_invocation,
    resolve_options,
    run_install,
)

BOM = b"\xef\xbb\xbf"
FAKE_KEY = "sk-ant-test-fake"


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestTomlGeneration:
    def test_no_bom_in_generated_toml(self, tmp_path: Path) -> None:
        toml = generate_toml(
            db_url="sqlite:///./demo.db",
            db_name="demo",
            read_only=True,
            model="claude-sonnet-4-5-20250929",
            memory_dir=str(tmp_path / "chroma"),
        )
        path = tmp_path / "sqllens.toml"
        path.write_text(toml, encoding="utf-8")
        with path.open("rb") as f:
            assert f.read(3) != BOM

    def test_round_trips_through_config_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = generate_toml(
            db_url="sqlite:///./demo.db",
            db_name="demo",
            read_only=True,
            model="claude-sonnet-4-5-20250929",
            memory_dir=str(tmp_path / "chroma"),
        )
        path = tmp_path / "sqllens.toml"
        path.write_text(toml, encoding="utf-8")
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.config import Config

        cfg = Config.load(path)
        assert cfg.database.url == "sqlite:///./demo.db"
        assert cfg.database.name == "demo"
        assert cfg.database.read_only is True
        assert cfg.llm.model == "claude-sonnet-4-5-20250929"

    def test_windows_path_with_backslashes_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOML literal strings (single-quoted) avoid the backslash-escape trap.
        windows_path = r"C:\Users\dan\sqllens\chroma"
        toml = generate_toml(
            db_url="sqlite:///./demo.db",
            db_name="demo",
            read_only=True,
            model="claude-sonnet-4-5-20250929",
            memory_dir=windows_path,
        )
        # Single-quoted literal string preserves backslashes verbatim.
        assert f"persist_dir = '{windows_path}'" in toml
        path = tmp_path / "sqllens.toml"
        path.write_text(toml, encoding="utf-8")
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.config import Config

        cfg = Config.load(path)
        # Path normalisation differs by host platform; just confirm load succeeded.
        assert str(cfg.memory.persist_dir).endswith("chroma")

    def test_toml_omits_api_key(self, tmp_path: Path) -> None:
        toml = generate_toml(
            db_url="sqlite:///./demo.db",
            db_name="demo",
            read_only=True,
            model="claude-sonnet-4-5-20250929",
            memory_dir=str(tmp_path / "chroma"),
        )
        # Any line that mentions api_key as a key (start-of-line, optionally
        # indented) must be a TOML comment, not an assignment. The substring
        # match also catches paths like ".../test_toml_omits_api_key0/..." so
        # we anchor to the start of the stripped line.
        for line in toml.splitlines():
            stripped = line.lstrip()
            if stripped.lower().startswith("api_key"):
                pytest.fail(f"uncommented api_key assignment: {line!r}")
            if stripped.startswith("#") and "api_key" in stripped.lower():
                continue
        assert FAKE_KEY not in toml


class TestPathDetection:
    def test_default_working_dir_windows(self) -> None:
        env = {"USERPROFILE": r"C:\Users\dan"}
        result = default_working_dir("win32", env)
        # PureWindowsPath comparison so the test works on Linux/macOS hosts.
        assert PureWindowsPath(str(result)) == PureWindowsPath(r"C:\Users\dan\sqllens")

    def test_default_working_dir_macos(self) -> None:
        result = default_working_dir("darwin", {})
        assert result == Path.home() / ".sqllens"

    def test_default_working_dir_linux(self) -> None:
        result = default_working_dir("linux", {})
        assert result == Path.home() / ".sqllens"

    def test_default_memory_dir_macos(self) -> None:
        result = default_memory_dir("darwin", {})
        assert result == Path.home() / ".sqllens" / "chroma"

    def test_default_config_path_windows(self) -> None:
        env = {"APPDATA": r"C:\Users\dan\AppData\Roaming"}
        result = default_config_path("win32", env)
        assert result is not None
        assert PureWindowsPath(str(result)) == PureWindowsPath(
            r"C:\Users\dan\AppData\Roaming\Claude\claude_desktop_config.json"
        )

    def test_default_config_path_windows_missing_appdata(self) -> None:
        assert default_config_path("win32", {}) is None

    def test_default_config_path_macos(self) -> None:
        result = default_config_path("darwin", {})
        assert result == (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )

    def test_default_config_path_linux(self) -> None:
        result = default_config_path("linux", {})
        assert result == Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    def test_default_config_path_unknown_platform(self) -> None:
        assert default_config_path("freebsd", {}) is None


class TestDeriveDefaultName:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("sqlite:///./chinook.db", "chinook"),
            ("sqlite:////absolute/path/db.sqlite", "db"),
            ("sqlite:///./data.sqlite3", "data"),
            ("postgresql://u:p@h:5432/mydb", "mydb"),
            ("mysql+pymysql://u:p@h:3306/orders", "orders"),
        ],
    )
    def test_derives_from_dsn(self, url: str, expected: str) -> None:
        assert derive_default_name(url) == expected

    def test_falls_back_for_unparseable(self) -> None:
        assert derive_default_name("not a url") == "sqllens"


class TestResolveInvocation:
    def test_uses_path_when_found(self) -> None:
        result = resolve_invocation(
            platform_name="linux",
            which=lambda exe: "/usr/local/bin/sqllens" if exe == "sqllens" else None,
        )
        assert result.command == "/usr/local/bin/sqllens"
        assert result.args_prefix == ()
        assert result.used_python_module_fallback is False

    def test_falls_back_to_python_m_when_not_on_path(self) -> None:
        result = resolve_invocation(
            platform_name="linux",
            which=lambda exe: None,
            sys_executable="/usr/bin/python3",
        )
        assert result.command == "/usr/bin/python3"
        assert result.args_prefix == ("-m", "sqllens")
        assert result.used_python_module_fallback is True

    def test_windows_prefers_exe_suffix(self) -> None:
        seen: list[str] = []

        def which(name: str) -> str | None:
            seen.append(name)
            return r"C:\Python\Scripts\sqllens.exe" if name == "sqllens.exe" else None

        result = resolve_invocation(platform_name="win32", which=which)
        assert "sqllens.exe" in seen
        assert result.command.endswith("sqllens.exe")
        assert result.used_python_module_fallback is False


# ---------------------------------------------------------------------------
# JSON merge tests
# ---------------------------------------------------------------------------


def _entry(name: str) -> dict[str, Any]:
    return {"command": f"/usr/local/bin/{name}", "args": [], "env": {}}


class TestMergeMcpServers:
    def test_preserves_preferences_and_siblings(self) -> None:
        existing = {
            "preferences": {"theme": "dark", "notifications": True},
            "mcpServers": {
                "other-server": _entry("other-server"),
            },
        }
        merged, siblings = merge_into_mcp_servers(
            existing, name="sqllens", entry=_entry("sqllens")
        )
        assert merged["preferences"] == {"theme": "dark", "notifications": True}
        assert "other-server" in merged["mcpServers"]
        assert "sqllens" in merged["mcpServers"]
        assert siblings == 1

    def test_inserts_when_no_mcp_servers_block(self) -> None:
        existing = {"preferences": {"theme": "dark"}}
        merged, siblings = merge_into_mcp_servers(
            existing, name="sqllens", entry=_entry("sqllens")
        )
        assert merged["preferences"] == {"theme": "dark"}
        assert "sqllens" in merged["mcpServers"]
        assert siblings == 0

    def test_overwrites_same_name_entry(self) -> None:
        existing = {
            "mcpServers": {"sqllens": {"command": "OLD", "args": [], "env": {}}}
        }
        new_entry = {"command": "NEW", "args": ["serve"], "env": {"K": "V"}}
        merged, siblings = merge_into_mcp_servers(existing, name="sqllens", entry=new_entry)
        assert merged["mcpServers"]["sqllens"] == new_entry
        assert siblings == 0  # no other servers to preserve

    def test_handles_missing_file(self) -> None:
        merged, siblings = merge_into_mcp_servers(None, name="sqllens", entry=_entry("sqllens"))
        assert "sqllens" in merged["mcpServers"]
        assert siblings == 0

    def test_rejects_non_object_mcp_servers(self) -> None:
        with pytest.raises(InstallError, match=r"non-object 'mcpServers'.*got str"):
            merge_into_mcp_servers(
                {"mcpServers": "this should be a dict"},
                name="sqllens",
                entry=_entry("sqllens"),
            )


# ---------------------------------------------------------------------------
# End-to-end run_install tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_config_json(tmp_path: Path) -> Path:
    """Pre-existing Claude Desktop config with preferences + sibling server."""
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(
        json.dumps(
            {
                "preferences": {"theme": "dark"},
                "mcpServers": {"other-server": _entry("other-server")},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def base_options(tmp_path: Path, fake_config_json: Path) -> InstallOptions:
    return InstallOptions(
        db_url="sqlite:///./demo.db",
        api_key=FAKE_KEY,
        name="demo",
        model="claude-sonnet-4-5-20250929",
        read_only=True,
        memory_dir=str(tmp_path / "chroma"),
        working_dir=tmp_path / "workdir",
        config_path=fake_config_json,
    )


def _fixed_now() -> datetime:
    return datetime(2026, 5, 5, 17, 42, 11, tzinfo=UTC)


class TestRunInstall:
    def test_writes_artifacts_on_linux(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        result = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        toml_path = base_options.working_dir / "sqllens.toml"
        assert toml_path.exists()
        assert toml_path.read_bytes()[:3] != BOM
        assert result.cmd_path is None  # no .cmd on Linux
        merged = json.loads(fake_config_json.read_text(encoding="utf-8"))
        assert merged["preferences"] == {"theme": "dark"}
        assert "other-server" in merged["mcpServers"]
        assert merged["mcpServers"]["demo"]["command"] == "/usr/local/bin/sqllens"
        assert merged["mcpServers"]["demo"]["args"] == ["serve", "-c", str(toml_path)]
        assert merged["mcpServers"]["demo"]["env"]["SQLLENS_LLM__API_KEY"] == FAKE_KEY
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert ".bak." in result.backup_path.name

    def test_writes_cmd_launcher_on_windows(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        result = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="win32",
            which=lambda exe: r"C:\Python\Scripts\sqllens.exe" if "sqllens" in exe else None,
            now=_fixed_now,
        )
        assert result.cmd_path is not None
        assert result.cmd_written is True
        cmd_body = result.cmd_path.read_text(encoding="utf-8")
        assert "@echo off" in cmd_body
        assert "cd /d" in cmd_body
        assert "sqllens.exe" in cmd_body
        merged = json.loads(fake_config_json.read_text(encoding="utf-8"))
        # JSON points at the .cmd, not the .exe directly.
        assert merged["mcpServers"]["demo"]["command"] == str(result.cmd_path)
        assert merged["mcpServers"]["demo"]["args"] == []

    def test_python_module_fallback_in_json(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        result = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: None,
            now=_fixed_now,
        )
        merged = json.loads(fake_config_json.read_text(encoding="utf-8"))
        entry = merged["mcpServers"]["demo"]
        assert entry["args"][:2] == ["-m", "sqllens"]
        assert entry["args"][2:] == ["serve", "-c", str(base_options.working_dir / "sqllens.toml")]
        assert result.used_python_module_fallback is True

    def test_idempotent_double_run(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        first = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        first_json = json.loads(fake_config_json.read_text(encoding="utf-8"))
        assert first.toml_written is True
        assert first.backup_path is not None
        second = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=lambda: datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC),
        )
        second_json = json.loads(fake_config_json.read_text(encoding="utf-8"))
        assert first_json == second_json
        # One entry for sqllens, one for other-server — never doubled.
        assert list(second_json["mcpServers"].keys()) == ["other-server", "demo"]
        # No-op rerun must not produce a second backup file or rewrite the TOML.
        assert second.toml_written is False
        assert second.backup_path is None
        bak_files = list(fake_config_json.parent.glob("claude_desktop_config.json.bak.*"))
        assert len(bak_files) == 1

    def test_dry_run_writes_nothing(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        before_files = set(base_options.config_path.parent.iterdir())
        result = run_install(
            base_options,
            dry_run=True,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        after_files = set(base_options.config_path.parent.iterdir())
        # No new files in the config dir.
        assert before_files == after_files
        # Working dir was not created.
        assert not (base_options.working_dir / "sqllens.toml").exists()
        # JSON on disk is unchanged.
        json_on_disk = json.loads(fake_config_json.read_text(encoding="utf-8"))
        assert "demo" not in json_on_disk["mcpServers"]
        # Result captures the plan.
        assert result.dry_run is True
        assert result.toml_content
        assert result.json_diff
        assert "demo" in result.json_after["mcpServers"]

    def test_backup_written_when_config_exists(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        original = fake_config_json.read_text(encoding="utf-8")
        result = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert result.backup_path.read_text(encoding="utf-8") == original
        assert re.search(r"\.bak\.\d{14}$", result.backup_path.name)

    def test_malformed_json_errors(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        fake_config_json.write_text("{ not valid json", encoding="utf-8")
        with pytest.raises(InstallError, match="not valid JSON"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )

    def test_non_object_top_level_json_errors(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        fake_config_json.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(InstallError, match="does not contain a JSON object"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )

    def test_empty_file_treated_as_empty_object(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty (or whitespace-only) config is a no-op preferences case;
        # the merge should succeed and add only the new server.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        fake_config_json.write_text("   \n", encoding="utf-8")
        run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        merged = json.loads(fake_config_json.read_text(encoding="utf-8"))
        assert "demo" in merged["mcpServers"]
        assert set(merged.keys()) == {"mcpServers"}

    def test_missing_config_path_errors(
        self, base_options: InstallOptions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        opts = dataclasses.replace(
            base_options, config_path=tmp_path / "does-not-exist.json"
        )
        with pytest.raises(InstallError, match="Claude Desktop config not found"):
            run_install(
                opts,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )

    def test_force_required_to_overwrite_existing_toml(
        self, base_options: InstallOptions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        base_options.working_dir.mkdir(parents=True, exist_ok=True)
        (base_options.working_dir / "sqllens.toml").write_text(
            "# hand-edited stuff\n", encoding="utf-8"
        )
        with pytest.raises(InstallError, match="already exists with different content"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )
        # --force overrides:
        result = run_install(
            base_options,
            dry_run=False,
            force=True,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        assert result.toml_written is True

    def test_invalid_toml_leaves_json_untouched_and_reverts_toml(
        self,
        base_options: InstallOptions,
        fake_config_json: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        # Patch the validator to raise so we exercise the revert path without
        # crafting a malformed TOML.
        original_json = fake_config_json.read_text(encoding="utf-8")
        from sqllens.installers import claude_desktop as installer_mod

        def boom(*_: object, **__: object) -> None:
            # ValueError stays in pydantic's family (ValidationError inherits
            # from it), so the test exercises the same broad-catch behaviour
            # the production code will hit.
            raise ValueError("synthetic validation failure")

        monkeypatch.setattr(installer_mod, "validate_toml", boom)

        with pytest.raises(InstallError, match="failed validation"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )
        # JSON unchanged.
        assert fake_config_json.read_text(encoding="utf-8") == original_json
        # TOML reverted (didn't exist before, so it should be gone).
        assert not (base_options.working_dir / "sqllens.toml").exists()

    def test_validation_failure_with_existing_toml_restores_original(
        self,
        base_options: InstallOptions,
        fake_config_json: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Exercises _revert_toml's "restore original" branch (not the unlink one).
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        base_options.working_dir.mkdir(parents=True, exist_ok=True)
        hand_edited = "# user hand-edited\nfoo = 1\n"
        toml_path = base_options.working_dir / "sqllens.toml"
        toml_path.write_text(hand_edited, encoding="utf-8")
        original_json = fake_config_json.read_text(encoding="utf-8")
        from sqllens.installers import claude_desktop as installer_mod

        def boom(*_: object, **__: object) -> None:
            raise ValueError("synthetic validation failure")

        monkeypatch.setattr(installer_mod, "validate_toml", boom)
        with pytest.raises(InstallError, match="failed validation"):
            run_install(
                base_options,
                dry_run=False,
                force=True,  # allow overwrite, then we expect a revert
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )
        # User's hand-edited TOML must come back, not the generated one.
        assert toml_path.read_text(encoding="utf-8") == hand_edited
        assert fake_config_json.read_text(encoding="utf-8") == original_json

    def test_validation_failure_on_windows_reverts_cmd_launcher(
        self,
        base_options: InstallOptions,
        fake_config_json: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # On Windows, the .cmd launcher is written before validate_toml runs.
        # A validation failure must clean it up — the contract is "never
        # half-applied state".
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.installers import claude_desktop as installer_mod

        def boom(*_: object, **__: object) -> None:
            raise ValueError("synthetic validation failure")

        monkeypatch.setattr(installer_mod, "validate_toml", boom)
        original_json = fake_config_json.read_text(encoding="utf-8")
        toml_path = base_options.working_dir / "sqllens.toml"
        cmd_path = base_options.working_dir / "run-sqllens.cmd"
        with pytest.raises(InstallError, match="failed validation"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="win32",
                which=lambda _: r"C:\Python\Scripts\sqllens.exe",
                now=_fixed_now,
            )
        assert not toml_path.exists()
        assert not cmd_path.exists(), "orphan .cmd left after validation failure"
        assert fake_config_json.read_text(encoding="utf-8") == original_json

    def test_validation_failure_on_windows_restores_existing_cmd(
        self,
        base_options: InstallOptions,
        fake_config_json: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If a hand-edited .cmd existed before, --force overwrites it and
        # then validation fails: the original .cmd must come back.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        base_options.working_dir.mkdir(parents=True, exist_ok=True)
        cmd_path = base_options.working_dir / "run-sqllens.cmd"
        # Plain \n — Path.read_text translates \r\n on Linux hosts anyway,
        # so what the installer captures as "existing_cmd" is the \n form.
        original_cmd = "@echo off\necho hand edited\n"
        cmd_path.write_text(original_cmd, encoding="utf-8")
        from sqllens.installers import claude_desktop as installer_mod

        def boom(*_: object, **__: object) -> None:
            raise ValueError("synthetic validation failure")

        monkeypatch.setattr(installer_mod, "validate_toml", boom)
        with pytest.raises(InstallError, match="failed validation"):
            run_install(
                base_options,
                dry_run=False,
                force=True,
                platform_name="win32",
                which=lambda _: r"C:\Python\Scripts\sqllens.exe",
                now=_fixed_now,
            )
        assert cmd_path.read_text(encoding="utf-8") == original_cmd

    def test_cmd_conflict_reverts_toml(
        self,
        base_options: InstallOptions,
        fake_config_json: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If a hand-edited .cmd blocks the install, the just-written TOML must
        # not be left orphaned in the working dir.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        base_options.working_dir.mkdir(parents=True, exist_ok=True)
        cmd_path = base_options.working_dir / "run-sqllens.cmd"
        cmd_path.write_text("@echo off\r\necho hand edited\r\n", encoding="utf-8")
        toml_path = base_options.working_dir / "sqllens.toml"
        assert not toml_path.exists()  # fresh
        with pytest.raises(InstallError, match="already exists with different content"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="win32",
                which=lambda _: r"C:\Python\Scripts\sqllens.exe",
                now=_fixed_now,
            )
        # TOML must be reverted (didn't exist before, so it should be unlinked).
        assert not toml_path.exists()
        # JSON must be untouched.
        assert fake_config_json.read_text(encoding="utf-8").strip().startswith("{")

    def test_windows_python_module_fallback_in_cmd_launcher(
        self,
        base_options: InstallOptions,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The {Windows + sqllens not on PATH} combination must embed
        # "python -m sqllens" into the .cmd body, not bare "python".
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        result = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="win32",
            which=lambda _: None,
            now=_fixed_now,
        )
        assert result.cmd_content is not None
        assert "-m sqllens" in result.cmd_content
        assert "serve -c" in result.cmd_content
        # The .cmd should also CRLF-terminate every line, not just the first.
        assert result.cmd_content.count("\r\n") == 3
        assert "\n" not in result.cmd_content.replace("\r\n", "")


# ---------------------------------------------------------------------------
# resolve_options tests
# ---------------------------------------------------------------------------


class TestResolveOptions:
    def test_uses_env_api_key_when_flag_absent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        opts = resolve_options(
            db_url="sqlite:///./demo.db",
            api_key=None,
            name=None,
            model="claude-sonnet-4-5-20250929",
            read_only=True,
            memory_dir=None,
            working_dir=tmp_path / "workdir",
            config_path=cfg,
            platform_name="linux",
            env={"SQLLENS_LLM__API_KEY": "env-key"},
        )
        assert opts.api_key == "env-key"
        assert opts.name == "demo"  # derived from DSN

    def test_raises_when_no_api_key_anywhere(self, tmp_path: Path) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        with pytest.raises(InstallError, match="API key is required"):
            resolve_options(
                db_url="sqlite:///./demo.db",
                api_key=None,
                name=None,
                model="claude-sonnet-4-5-20250929",
                read_only=True,
                memory_dir=None,
                working_dir=tmp_path / "workdir",
                config_path=cfg,
                platform_name="linux",
                env={},
            )

    def test_raises_for_unknown_platform_without_config_path(self, tmp_path: Path) -> None:
        with pytest.raises(InstallError, match="Could not detect"):
            resolve_options(
                db_url="sqlite:///./demo.db",
                api_key=FAKE_KEY,
                name=None,
                model="claude-sonnet-4-5-20250929",
                read_only=True,
                memory_dir=None,
                working_dir=tmp_path / "workdir",
                config_path=None,
                platform_name="freebsd",
                env={},
            )

    def test_rejects_unparseable_dsn_upfront(self, tmp_path: Path) -> None:
        # A malformed --db value must fail at resolve_options, BEFORE the
        # installer writes a TOML and a .cmd that would only fail at the
        # later validate_toml step. Catches the "wrote files, then failed"
        # surprise the prior derive_default_name fallback silently allowed.
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        with pytest.raises(InstallError, match="not a valid SQLAlchemy URL"):
            resolve_options(
                db_url="this is not a dsn",
                api_key=FAKE_KEY,
                name=None,
                model="claude-sonnet-4-5-20250929",
                read_only=True,
                memory_dir=None,
                working_dir=tmp_path / "workdir",
                config_path=cfg,
                platform_name="linux",
                env={},
            )
        # Nothing was created.
        assert not (tmp_path / "workdir").exists()


# ---------------------------------------------------------------------------
# CLI integration via Typer's CliRunner
# ---------------------------------------------------------------------------


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return re.sub(r"\s+", " ", _ANSI.sub("", text))


class TestCli:
    def test_help_lists_every_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force a wide terminal so Rich doesn't wrap flag names across lines.
        monkeypatch.setenv("COLUMNS", "200")
        runner = CliRunner()
        result = runner.invoke(app, ["claude-desktop", "install", "--help"])
        assert result.exit_code == 0
        clean = _strip_ansi(result.stdout)
        for flag in (
            "--db",
            "--api-key",
            "--name",
            "--model",
            "--memory-dir",
            "--working-dir",
            "--config-path",
            "--read-only",
            "--no-read-only",
            "--dry-run",
            "--force",
        ):
            assert flag in clean, f"missing flag in --help: {flag}"

    def test_missing_required_db_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["claude-desktop", "install", "--api-key", FAKE_KEY])
        assert result.exit_code != 0

    def test_dry_run_via_cli_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        monkeypatch.setenv("COLUMNS", "200")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"preferences": {"theme": "dark"}}), encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "claude-desktop",
                "install",
                "--db",
                "sqlite:///./demo.db",
                "--working-dir",
                str(tmp_path / "workdir"),
                "--config-path",
                str(cfg),
                "--dry-run",
            ],
        )
        clean = _strip_ansi(result.stdout)
        assert result.exit_code == 0, clean
        assert "Dry run" in clean
        assert not (tmp_path / "workdir" / "sqllens.toml").exists()
        # JSON on disk unchanged.
        assert "demo" not in json.loads(cfg.read_text(encoding="utf-8")).get("mcpServers", {})

    def test_missing_config_path_via_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        monkeypatch.setenv("COLUMNS", "200")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "claude-desktop",
                "install",
                "--db",
                "sqlite:///./demo.db",
                "--working-dir",
                str(tmp_path / "workdir"),
                "--config-path",
                str(tmp_path / "does-not-exist.json"),
            ],
        )
        assert result.exit_code != 0
        assert "Claude Desktop config not found" in _strip_ansi(result.stdout)

    def test_post_install_output_contains_required_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        monkeypatch.setenv("COLUMNS", "200")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "claude-desktop",
                "install",
                "--db",
                "sqlite:///./demo.db",
                "--working-dir",
                str(tmp_path / "workdir"),
                "--config-path",
                str(cfg),
            ],
        )
        clean = _strip_ansi(result.stdout)
        assert result.exit_code == 0, clean
        assert str(cfg) in clean
        assert str(tmp_path / "workdir") in clean
        assert "plaintext" in clean
        assert "Restart Claude Desktop" in clean


# ---------------------------------------------------------------------------
# Helpers used in artifact generation
# ---------------------------------------------------------------------------


class TestCmdLauncher:
    def test_renders_quoted_paths(self) -> None:
        out = generate_cmd_launcher(
            working_dir=Path(r"C:\Users\dan\sqllens"),
            server_command=r"C:\Python\Scripts\sqllens.exe",
            server_args=["serve", "-c", r"C:\Users\dan\sqllens\sqllens.toml"],
        )
        assert out.startswith("@echo off\r\n")
        assert "cd /d " in out
        assert r"sqllens.exe" in out
        assert "sqllens.toml" in out

    def test_quotes_paths_with_spaces(self) -> None:
        out = generate_cmd_launcher(
            working_dir=Path(r"C:\Users\with space\sqllens"),
            server_command=r"C:\Program Files\Python\sqllens.exe",
            server_args=["serve", "-c", r"C:\Users\with space\sqllens\sqllens.toml"],
        )
        assert '"C:\\Users\\with space\\sqllens"' in out
        assert '"C:\\Program Files\\Python\\sqllens.exe"' in out


class TestBackupPath:
    def test_make_backup_path_appends_timestamp(self, tmp_path: Path) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        result = make_backup_path(cfg, _fixed_now())
        assert result.name == "claude_desktop_config.json.bak.20260505174211"


# ---------------------------------------------------------------------------
# Regression: Windows idempotency under CRLF round-trip (PR #25 follow-up)
# ---------------------------------------------------------------------------


class TestWindowsIdempotency:
    def test_idempotent_double_run_windows(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # generate_cmd_launcher emits CRLF; Path.read_text would collapse to LF
        # under universal newlines, so the second run would otherwise demand
        # --force. Comparing bytes pins the no-op.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        first = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="win32",
            which=lambda exe: r"C:\Python\Scripts\sqllens.exe" if "sqllens" in exe else None,
            now=_fixed_now,
        )
        assert first.cmd_written is True
        second = run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="win32",
            which=lambda exe: r"C:\Python\Scripts\sqllens.exe" if "sqllens" in exe else None,
            now=lambda: datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC),
        )
        # No --force needed, no rewrite, no second backup.
        assert second.cmd_written is False
        assert second.toml_written is False
        assert second.backup_path is None
        # And the on-disk bytes still carry CRLF.
        assert first.cmd_path is not None
        raw = first.cmd_path.read_bytes()
        assert b"\r\n" in raw

    def test_dry_run_works_without_existing_claude_config(
        self, base_options: InstallOptions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fresh machine: Claude Desktop config doesn't exist yet; --dry-run
        # should still preview the plan instead of erroring.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        missing = tmp_path / "no-such" / "claude_desktop_config.json"
        opts = dataclasses.replace(base_options, config_path=missing)
        result = run_install(
            opts,
            dry_run=True,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        assert result.dry_run is True
        assert "demo" in result.json_after["mcpServers"]
        assert not missing.exists()


class TestRestoreHint:
    def test_uses_copy_on_windows(self, tmp_path: Path) -> None:
        from sqllens.installers.claude_desktop import _restore_hint

        hint = _restore_hint("win32", tmp_path / "x.bak", tmp_path / "x.json")
        assert hint.startswith("copy ")
        assert hint.count('"') == 4  # both paths quoted

    def test_uses_cp_on_unix(self, tmp_path: Path) -> None:
        from sqllens.installers.claude_desktop import _restore_hint

        hint = _restore_hint("linux", tmp_path / "x.bak", tmp_path / "x.json")
        assert hint.startswith("cp ")


class TestReadExistingConfig:
    def test_oserror_translates_to_install_error(
        self, base_options: InstallOptions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-FileNotFoundError OSError (e.g. permission denied) must hit the
        # InstallError channel rather than the CLI's "file an issue" backstop.
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.installers import claude_desktop as installer_mod

        def boom_read(*_: object, **__: object) -> str:
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr(Path, "read_text", boom_read)
        with pytest.raises(InstallError, match="Could not read"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )
        # silence unused import warning
        _ = installer_mod


class TestAtomicJsonWrite:
    def test_no_tmp_file_left_on_success(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        # Atomic write uses a tempfile in the config dir; on success it must
        # be renamed away, not left behind.
        tmps = list(fake_config_json.parent.glob("claude_desktop_config.json.*.tmp"))
        assert tmps == []

    def test_failed_write_cleans_up_tmp_and_keeps_backup(
        self, base_options: InstallOptions, fake_config_json: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.installers import claude_desktop as installer_mod

        original_replace = installer_mod.os.replace

        def boom_replace(*args: object, **kwargs: object) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(installer_mod.os, "replace", boom_replace)
        with pytest.raises(InstallError, match="If it is missing or corrupt, restore it with"):
            run_install(
                base_options,
                dry_run=False,
                force=False,
                platform_name="linux",
                which=lambda _: "/usr/local/bin/sqllens",
                now=_fixed_now,
            )
        # No tmp left behind.
        tmps = list(fake_config_json.parent.glob("claude_desktop_config.json.*.tmp"))
        assert tmps == []
        # Backup still present.
        baks = list(fake_config_json.parent.glob("claude_desktop_config.json.bak.*"))
        assert len(baks) == 1
        # silence
        _ = original_replace


class TestTomlStringDoubleQuotedFallback:
    def test_password_with_apostrophe_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A DSN containing a single quote forces _toml_string into its
        # double-quoted basic-string fallback. The generated TOML must still
        # parse cleanly back through Config.load.
        url_with_apostrophe = "sqlite:///./demo's.db"
        toml = generate_toml(
            db_url=url_with_apostrophe,
            db_name="demo",
            read_only=True,
            model="claude-sonnet-4-5-20250929",
            memory_dir=str(tmp_path / "chroma"),
        )
        # Must have picked the double-quoted form for this field.
        assert 'url = "sqlite:///./demo\'s.db"' in toml
        path = tmp_path / "sqllens.toml"
        path.write_text(toml, encoding="utf-8")
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        from sqllens.config import Config

        cfg = Config.load(path)
        assert cfg.database.url == url_with_apostrophe


class TestRevertVisibility:
    """Revert handlers must not silently swallow secondary OSErrors.

    Prior to the iteration-3 fix, ``_revert_toml`` / ``_revert_cmd_bytes``
    used a bare ``except OSError: pass``. If the revert itself failed (the
    documented case: antivirus has the freshly-written file locked) the
    user got *no* signal — Typer's pretty error renderer doesn't surface
    ``__context__``. These tests pin that a warning lands on stderr.
    """

    def test_revert_toml_warns_on_secondary_oserror(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from sqllens.installers.claude_desktop import _revert_toml

        toml_path = tmp_path / "sqllens.toml"
        toml_path.write_text("# new content", encoding="utf-8")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("simulated antivirus lock")

        # Patch the unlink path (original was None -> revert calls unlink).
        original_unlink = Path.unlink
        try:
            Path.unlink = boom  # type: ignore[method-assign]
            _revert_toml(toml_path, original=None)
        finally:
            Path.unlink = original_unlink  # type: ignore[method-assign]

        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert str(toml_path) in captured.err
        assert "inconsistent state" in captured.err

    def test_revert_cmd_bytes_warns_on_secondary_oserror(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from sqllens.installers.claude_desktop import _revert_cmd_bytes

        cmd_path = tmp_path / "run-sqllens.cmd"
        cmd_path.write_bytes(b"@echo new\r\n")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("simulated antivirus lock")

        original_write_bytes = Path.write_bytes
        try:
            Path.write_bytes = boom  # type: ignore[method-assign]
            _revert_cmd_bytes(cmd_path, original=b"@echo old\r\n")
        finally:
            Path.write_bytes = original_write_bytes  # type: ignore[method-assign]

        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert str(cmd_path) in captured.err


class TestCliUnexpectedError:
    def test_unexpected_error_exits_with_code_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI's ``except Exception`` backstop must use a clean Typer Exit.

        A bare ``raise`` would dump a Python traceback right after the
        friendly "this is likely a bug" framing, contradicting the framing
        and producing dual output. The fix swaps the bare raise for
        ``raise typer.Exit(code=2) from exc``.
        """
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        monkeypatch.setenv("COLUMNS", "200")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")

        from sqllens.installers import claude_desktop as installer_mod

        def boom_run(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setattr(installer_mod, "run_install", boom_run)
        # Re-import the symbol the CLI uses to ensure the patch lands on the
        # name the CLI's local-import resolves at call time.
        import sqllens.cli  # noqa: F401

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "claude-desktop",
                "install",
                "--db",
                "sqlite:///./demo.db",
                "--working-dir",
                str(tmp_path / "workdir"),
                "--config-path",
                str(cfg),
            ],
        )
        assert result.exit_code == 2, (
            f"expected exit 2 from typer.Exit, got {result.exit_code}\n"
            f"stdout={result.stdout}\nexception={result.exception}"
        )
        clean = _strip_ansi(result.stdout)
        assert "Unexpected error" in clean
        assert "RuntimeError" in clean
        assert "file an issue" in clean
