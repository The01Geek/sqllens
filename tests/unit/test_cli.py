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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("serve", "Config error"),
        ("validate", "Invalid"),
    ],
)
def test_config_load_failure_goes_to_stderr(tmp_path, command: str, expected: str) -> None:
    # Stdio MCP clients read JSON-RPC on stdout; operator errors must land on
    # stderr to avoid corrupting that stream. Assert stdout is completely
    # empty — the contract is "no non-JSON-RPC bytes on stdout", not just
    # "no specific error substring on stdout".
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, [command, "--config", str(missing)])
    assert result.exit_code == 2
    assert expected in result.stderr
    assert result.stdout == ""


def test_init_already_exists_error_goes_to_stderr(tmp_path) -> None:
    # Same stdio-safety contract: the `init` "already exists" error must
    # land on stderr, never on stdout.
    existing = tmp_path / "sqllens.toml"
    existing.write_text("# placeholder\n")
    result = runner.invoke(app, ["init", "--path", str(existing)])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert "already exists" not in result.stdout
