# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the `sqllens serve` CLI entrypoint."""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from sqllens.cli import app


def test_serve_exits_when_api_key_missing(tmp_path: Path, monkeypatch) -> None:
    """`sqllens serve` must exit 2 with a clear message naming both the env var and TOML field."""
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--config", str(cfg_path)])
    assert result.exit_code == 2
    assert "SQLLENS_LLM__API_KEY" in result.output
    assert "api_key" in result.output
    assert "[llm]" in result.output
