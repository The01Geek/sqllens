# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Synthetic configuration destinations and diagnostic canaries only."""

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqllens.cli import app
from sqllens.config import Config, ConfigBomError
from sqllens.installers import claude_desktop as installer
from sqllens.preflight import PreflightError, probe_database

from .test_cli_claude_desktop import _fixed_now
from .test_cli_claude_desktop import base_options as base_options
from .test_cli_claude_desktop import fake_config_json as fake_config_json
from .test_preflight import _cfg


def _install(options):
    return installer.run_install(
        options,
        dry_run=False,
        force=True,
        platform_name="win32",
        which=lambda _: "/synthetic/sqllens",
        now=_fixed_now,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_artifacts_are_private_under_permissive_umask(base_options, monkeypatch) -> None:
    old_mask = os.umask(0)
    replacements = []
    original_replace = os.replace

    def checked_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        replacements.append(Path(destination).name)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", checked_replace)
    try:
        result = _install(base_options)
        paths = [
            base_options.working_dir / "sqllens.toml",
            result.cmd_path,
            base_options.config_path,
            result.backup_path,
        ]
        for path in paths:
            assert path is not None
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert {path.name for path in paths} <= set(replacements)
    finally:
        os.umask(old_mask)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("existing", [False, True])
def test_init_creates_or_replaces_private_config(tmp_path, existing: bool) -> None:
    path = tmp_path / "sample.toml"
    if existing:
        path.write_text("old")
        path.chmod(0o666)
    old_mask = os.umask(0)
    try:
        result = CliRunner().invoke(app, ["init", "--path", str(path), "--force"])
    finally:
        os.umask(old_mask)
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["toml", "launcher", "json", "backup"])
@pytest.mark.parametrize("broken", [False, True])
def test_installer_rejects_symlink_destinations_before_mutation(
    base_options,
    tmp_path,
    kind: str,
    broken: bool,
) -> None:
    base_options.working_dir.mkdir()
    paths = {
        "toml": base_options.working_dir / "sqllens.toml",
        "launcher": base_options.working_dir / "run-sqllens.cmd",
        "json": base_options.config_path,
        "backup": installer.make_backup_path(base_options.config_path, _fixed_now()),
    }
    target = tmp_path / "protected-target"
    if not broken:
        target.write_bytes(b"{}")
    path = paths[kind]
    path.unlink(missing_ok=True)
    path.symlink_to(target)
    with pytest.raises(installer.InstallError, match="Refusing symlink destination"):
        _install(base_options)
    assert path.is_symlink()
    assert target.read_bytes() == b"{}" if not broken else not target.exists()


@pytest.mark.parametrize("invalid", [False, True])
def test_validation_never_assigns_supplied_api_key_to_environment(
    tmp_path, monkeypatch, invalid
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("invalid = [" if invalid else '[database]\nurl="sqlite:///:memory:"\n')
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "existing-synthetic-key")
    written = []
    original_set = os._Environ.__setitem__

    def record(env, key, value):
        written.append((key, value))
        original_set(env, key, value)

    monkeypatch.setattr(os._Environ, "__setitem__", record)
    if invalid:
        with pytest.raises(ValueError):
            installer.validate_toml(path, api_key="supplied-synthetic-key")
    else:
        installer.validate_toml(path, api_key="supplied-synthetic-key")
    assert ("SQLLENS_LLM__API_KEY", "supplied-synthetic-key") not in written
    assert os.environ["SQLLENS_LLM__API_KEY"] == "existing-synthetic-key"


def test_config_load_key_override_preserves_other_sources(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[database]\nurl="sqlite:///:memory:"\n[llm]\nmodel="model-from-file"\nmax_tokens=200\n'
    )
    monkeypatch.setenv("SQLLENS_LLM__MAX_TOKENS", "300")
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "environment-key")
    cfg = Config.load(path, llm_api_key="explicit-key")
    assert cfg.llm.api_key.get_secret_value() == "explicit-key"
    assert cfg.llm.model == "model-from-file" and cfg.llm.max_tokens == 300
    assert cfg.database.url == "sqlite:///:memory:"


@pytest.mark.parametrize(
    "url", ["PRIVATE_CANARY", "PRIVATE_CANARY://x", "mysql://user:pw@localhost:PRIVATE_CANARY/db"]
)
def test_database_url_diagnostics_withhold_values(url: str) -> None:
    with pytest.raises(PreflightError) as error:
        probe_database(_cfg(db_url=url))
    assert "private_canary" not in str(error.value).lower()


@pytest.mark.parametrize(
    "module,url,error_name",
    [
        ("sqlite3", "sqlite:///:memory:", "OperationalError"),
        ("psycopg2", "postgresql://user:pw@localhost/db", "OperationalError"),
        ("pymysql", "mysql://user:pw@localhost/db", "MySQLError"),
    ],
)
def test_database_driver_diagnostics_withhold_values(monkeypatch, module, url, error_name) -> None:
    driver = pytest.importorskip(module)
    original = getattr(driver, error_name)("PRIVATE_CANARY")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(driver, "connect", fail)
    with pytest.raises(PreflightError) as error:
        probe_database(_cfg(db_url=url))
    assert "private_canary" not in str(error.value).lower()
    assert error.value.__cause__ is original


def test_installer_invalid_port_diagnostic_withholds_value(base_options) -> None:
    with pytest.raises(installer.InstallError) as error:
        installer.resolve_options(
            db_url="postgresql://synthetic:pw@localhost:PRIVATE_CANARY/db",
            api_key=base_options.api_key,
            name="test",
            model=base_options.model,
            read_only=True,
            memory_dir=None,
            working_dir=base_options.working_dir,
            config_path=base_options.config_path,
            platform_name="linux",
            env={},
        )
    assert "PRIVATE_CANARY" not in str(error.value)
    assert isinstance(error.value.__cause__, ValueError)


def test_explicit_key_does_not_bypass_bom_diagnostic(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'\xef\xbb\xbf[database]\nurl="sqlite:///:memory:"\n')
    with pytest.raises(ConfigBomError):
        Config.load(path, llm_api_key="explicit-synthetic-key")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("kind", ["toml", "cmd"])
def test_rollback_replaces_content_privately(tmp_path, kind) -> None:
    path = tmp_path / "destination"
    path.write_text("new content")
    path.chmod(0o666)
    if kind == "toml":
        installer._revert_toml(path, "old content\n")
        assert path.read_bytes() == b"old content\n"
    else:
        installer._revert_cmd_bytes(path, b"old content\r\n")
        assert path.read_bytes() == b"old content\r\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
