# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer CLI surface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqllens import __version__
from sqllens.cli import app

runner = CliRunner()


def _write_config(
    path: Path,
    *,
    db_url: str = "sqlite:///:memory:",
    auth_block: str = '[auth]\nmode = "none"\n',
    memory_dir: str | None = None,
) -> Path:
    memory_block = ""
    if memory_dir is not None:
        memory_block = f'\n[memory]\npersist_dir = "{memory_dir}"\n'
    path.write_text(
        f"""
[database]
url = "{db_url}"
name = "primary"

[llm]
api_key = "sk-ant-test"
{memory_block}
{auth_block}
"""
    )
    return path


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout


def test_version_subcommand_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout


def test_version_flag_short_circuits_before_subcommand() -> None:
    result = runner.invoke(app, ["--version", "serve"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout
    assert "Config error" not in result.stdout


def test_no_args_prints_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Natural-language SQL analytics over MCP." in result.stdout
    assert "serve" in result.stdout
    assert "init" in result.stdout
    assert "validate" in result.stdout
    assert "version" in result.stdout


# ---------------------------------------------------------------------------
# Preflight integration with `serve` and `validate`
# ---------------------------------------------------------------------------


def test_serve_preflight_blocks_on_unwritable_persist_dir(tmp_path: Path) -> None:
    # Point persist_dir at a path whose parent is a file — mkdir raises NotADirectory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        memory_dir=str(blocker / "chroma"),
    )

    # Stop before the server actually starts; preflight runs first anyway.
    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 2
    assert "Preflight failed:" in result.stdout
    assert "memory" in result.stdout
    mock_run.assert_not_called()


def test_serve_preflight_blocks_on_bearer_without_token(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        auth_block='[auth]\nmode = "bearer"\n',
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 2
    assert "Preflight failed:" in result.stdout
    assert "auth" in result.stdout
    mock_run.assert_not_called()


def test_serve_preflight_blocks_on_bad_database(tmp_path: Path) -> None:
    # SQLite refuses to open a database file inside a non-existent directory.
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 2
    assert "Preflight failed:" in result.stdout
    assert "database" in result.stdout
    mock_run.assert_not_called()


def test_serve_no_preflight_flag_skips_probes(tmp_path: Path) -> None:
    # Same broken DB as above, but --no-preflight should skip and let run() execute.
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path), "--no-preflight"])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_serve_no_preflight_env_var_skips_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )
    monkeypatch.setenv("SQLLENS_NO_PREFLIGHT", "1")

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_serve_preflight_passes_on_clean_sqlite_config(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url="sqlite:///:memory:",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_validate_check_flags_exercise_probes(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url="sqlite:///:memory:",
        memory_dir=str(tmp_path / "chroma"),
    )

    result = runner.invoke(
        app,
        [
            "validate",
            "--config",
            str(cfg_path),
            "--check-db",
            "--check-llm",
            "--check-memory",
            "--check-auth",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "database OK" in result.stdout
    assert "llm OK" in result.stdout
    assert "memory OK" in result.stdout
    assert "auth OK" in result.stdout


def test_validate_check_db_reports_failure(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing' / 'x.db'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    result = runner.invoke(app, ["validate", "--config", str(cfg_path), "--check-db"])

    assert result.exit_code == 2
    assert "Preflight failed:" in result.stdout
    assert "database" in result.stdout
