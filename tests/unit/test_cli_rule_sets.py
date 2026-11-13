# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``sqllens validate``, ``serve`` and ``verify-memory`` with a rule-set file (issue #374)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqllens.cli import app

from ._rule_set_fixtures import MYSQL_URL, example_document, to_toml

runner = CliRunner()


def _write(tmp_path: Path, document: dict[str, Any] | None = None, url: str = MYSQL_URL) -> Path:
    rules = tmp_path / "rules.toml"
    rules.write_text(to_toml(document if document is not None else example_document()))
    cfg = tmp_path / "sqllens.toml"
    cfg.write_text(
        f'[database]\nurl = "{url}"\n'
        '[llm]\napi_key = "sk-ant-test"\n'
        f'[memory]\npersist_dir = "{(tmp_path / "chroma").as_posix()}"\n'
        f'[rule_sets]\nfile = "{rules.as_posix()}"\n'
    )
    return cfg


def _unwrapped(text: str) -> str:
    """Console output with rich's line wrapping undone."""
    return " ".join(text.split())


def _confirmed_only() -> dict[str, Any]:
    document = example_document()
    document["rule_sets"] = document["rule_sets"][:1]
    return document


def test_validate_accepts_a_valid_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", "-c", str(_write(tmp_path, _confirmed_only()))])
    assert result.exit_code == 0, result.output
    assert "Config OK" in result.stdout
    assert "Warning:" not in result.stderr


def test_validate_warns_once_per_unconfirmed_set(tmp_path: Path) -> None:
    document = example_document()
    document["rule_sets"].append({**document["rule_sets"][1], "name": "8"})
    result = runner.invoke(app, ["validate", "-c", str(_write(tmp_path, document))])
    assert result.exit_code == 0, result.output
    warnings = [line for line in result.stderr.splitlines() if line.startswith("Warning:")]
    assert len(warnings) == 2
    assert "'3'" in warnings[0] and "'8'" in warnings[1]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["rule_sets"][0]["tables"][0].update(columns=["id", "name", "id"]),
         "rule_sets[0].tables[0]: duplicate column 'id'"),
        (lambda d: d["rule_sets"][0]["tables"][0].update(row_rule={"column": "rep_id"}),
         "remove the whole rule set"),
        (lambda d: d["binding"].update(database="primary"),
         "is not the database named in database.url"),
        (lambda d: d["metadata"].update(rule_set="ui_features_available"), "reserved"),
    ],
)
def test_validate_and_serve_refuse_a_bad_file(tmp_path: Path, mutate, expected: str) -> None:
    document = example_document()
    mutate(document)
    cfg = _write(tmp_path, document)
    result = runner.invoke(app, ["validate", "-c", str(cfg)])
    assert result.exit_code == 2
    assert expected in _unwrapped(result.stderr)
    with patch("sqllens.server.run") as run:
        served = runner.invoke(app, ["serve", "-c", str(cfg), "--no-preflight"])
    assert served.exit_code == 2
    assert expected in _unwrapped(served.stderr)
    run.assert_not_called()


def test_validate_refuses_a_non_mysql_url(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", "-c", str(_write(tmp_path, url="sqlite:///x.db"))])
    assert result.exit_code == 2
    assert "need a MySQL database" in _unwrapped(result.stderr)


def test_serve_logs_one_warning_per_unconfirmed_set_and_starts(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="sqllens.rule_sets"):
        with patch("sqllens.server.run") as run:
            result = runner.invoke(
                app, ["serve", "-c", str(_write(tmp_path)), "--no-preflight"]
            )
    assert result.exit_code == 0, result.output
    run.assert_called_once()
    warnings = [r.getMessage() for r in caplog.records if r.name == "sqllens.rule_sets"]
    assert len(warnings) == 1 and "'3'" in warnings[0]


def test_serve_preflight_runs_the_catalog_check(tmp_path: Path) -> None:
    from sqllens.preflight import PreflightError

    def failing(cfg):
        raise PreflightError("database", "the rule-set file does not match the database: "
                             "column customers.name does not exist")

    with patch("sqllens.preflight.probe_database", failing), patch(
        "sqllens.preflight._PROBES", (failing,)
    ), patch("sqllens.server.run") as run:
        result = runner.invoke(app, ["serve", "-c", str(_write(tmp_path))])
    assert result.exit_code == 2
    assert "customers.name" in _unwrapped(result.stderr)
    run.assert_not_called()


def test_verify_memory_refuses_before_running_any_case(tmp_path: Path) -> None:
    golden = tmp_path / "golden.json"
    golden.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 1"}]}')
    with patch("sqllens.eval.run_verification") as run:
        result = runner.invoke(
            app, ["verify-memory", str(golden), "-c", str(_write(tmp_path))]
        )
    assert result.exit_code != 0
    assert "does not support rule sets yet" in _unwrapped(result.stderr)
    run.assert_not_called()


async def test_run_verification_refuses_rule_sets(tmp_path: Path) -> None:
    from sqllens.eval import run_verification

    from ._rule_set_fixtures import rule_set_config

    with pytest.raises(ValueError, match="does not support rule sets yet"):
        await run_verification(rule_set_config(tmp_path), [])
