# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sqllens import __version__
from sqllens.cli import app

runner = CliRunner()


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


def test_serve_config_error_goes_to_stderr(tmp_path) -> None:
    """Operator error messages must not collide with stdout under the stdio MCP
    transport. Pointing serve at a non-existent config triggers the
    ``Config error:`` branch, which must land on stderr and leave stdout empty.
    """
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, ["serve", "--config", str(missing)])
    assert result.exit_code == 2
    assert "Config error" in result.stderr
    assert "Config error" not in result.stdout


def test_serve_missing_api_key_error_goes_to_stderr(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-on ``api_key`` check must also land on stderr."""
    cfg = tmp_path / "sqllens.toml"
    cfg.write_text(
        '[database]\nurl = "sqlite:///./demo.db"\nname = "primary"\n'
        '[llm]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-5-20250929"\n'
    )
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["serve", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "Config error" in result.stderr
    assert "Config error" not in result.stdout


def test_validate_invalid_config_error_goes_to_stderr(tmp_path) -> None:
    """The validate command's error path must also stay off stdout for
    consistency with serve — operators piping its stdout get clean output."""
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, ["validate", "--config", str(missing)])
    assert result.exit_code == 2
    assert "Invalid" in result.stderr
    assert "Invalid" not in result.stdout
