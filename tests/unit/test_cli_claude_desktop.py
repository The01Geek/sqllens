# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `sqllens claude-desktop install`.

Tests target the installer module directly (pure-function path) plus the CLI
glue (via Typer's CliRunner). No real Claude Desktop, network, or DB access.
"""

from __future__ import annotations

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

# Hostile-env pollution: some CI runners export unprefixed names (MODE, HOST,
# PORT, BEARER_TOKEN, ...) that nested sqllens BaseSettings sub-models read
# without the SQLLENS_ prefix. Scrub them so Config.load round-trips work.
_LEAKY_ENV_KEYS = (
    "MODE",
    "HOST",
    "PORT",
    "URL",
    "NAME",
    "API_KEY",
    "PROVIDER",
    "MODEL",
    "PERSIST_DIR",
    "COLLECTION",
    "SIMILARITY_THRESHOLD",
    "READ_ONLY",
    "BEARER_TOKEN",
    "JWT_JWKS_URL",
    "JWT_ISSUER",
    "JWT_AUDIENCE",
    "TRANSPORT",
)


@pytest.fixture(autouse=True)
def _scrub_leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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
        assert "api_key" not in toml.lower() or "# api_key" in toml.lower()
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
        assert result.args_prefix == []
        assert result.used_python_module_fallback is False

    def test_falls_back_to_python_m_when_not_on_path(self) -> None:
        result = resolve_invocation(
            platform_name="linux",
            which=lambda exe: None,
            sys_executable="/usr/bin/python3",
        )
        assert result.command == "/usr/bin/python3"
        assert result.args_prefix == ["-m", "sqllens"]
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
        with pytest.raises(InstallError, match="non-object 'mcpServers'"):
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
        run_install(
            base_options,
            dry_run=False,
            force=False,
            platform_name="linux",
            which=lambda _: "/usr/local/bin/sqllens",
            now=_fixed_now,
        )
        first_json = json.loads(fake_config_json.read_text(encoding="utf-8"))
        run_install(
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

    def test_missing_config_path_errors(
        self, base_options: InstallOptions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQLLENS_LLM__API_KEY", FAKE_KEY)
        opts = InstallOptions(
            **{**base_options.__dict__, "config_path": tmp_path / "does-not-exist.json"}
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
        # Construct a bogus DSN that Config.load will accept syntactically but
        # let's instead simulate by patching Config.load via the module path:
        original_json = fake_config_json.read_text(encoding="utf-8")
        from sqllens.installers import claude_desktop as installer_mod

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("synthetic validation failure")

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
