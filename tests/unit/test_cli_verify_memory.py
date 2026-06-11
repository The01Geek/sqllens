# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CLI tests for ``sqllens verify-memory``.

The agent path is patched at the runner seam so these tests run without an
LLM key or a database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqllens.cli import app

runner = CliRunner()


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "sqllens.toml"
    cfg.write_text(
        f"""
[database]
url = "sqlite:///:memory:"
name = "primary"

[llm]
api_key = "sk-ant-test"

[memory]
persist_dir = "{tmp_path / 'chroma'}"

[auth]
mode = "none"
"""
    )
    return cfg


def _golden(tmp_path: Path, pairs: list[tuple[str, str]]) -> Path:
    import json

    golden = tmp_path / "golden.json"
    golden.write_text(
        json.dumps(
            {"sql_pairs": {"pairs": [{"question": q, "sql": s} for q, s in pairs]}}
        )
    )
    return golden


def _patch_driver(monkeypatch, responses: list) -> None:
    """Patch the runner's default driver path with a scripted responder.

    ``cli.verify_memory`` imports ``run_verification`` lazily via
    ``from sqllens.eval import ...`` inside the function body. The lazy
    import resolves the name as an attribute on the already-loaded
    ``sqllens.eval`` module at call time — so patching that attribute
    intercepts the call. A patch on ``sqllens.cli.run_verification`` would
    be ineffective because no such binding exists at module scope.
    """
    iterator = iter(responses)

    async def _drv(cfg, question):
        nxt = next(iterator)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def _runner_call(cfg, cases, *, driver=None):
        from sqllens.eval.runner import run_verification as real

        return await real(cfg, cases, driver=_drv)

    import sqllens.eval

    monkeypatch.setattr(sqllens.eval, "run_verification", _runner_call)


def test_all_pass_exits_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q1", "SELECT 1")])
    _patch_driver(monkeypatch, [("ok", [], {"sql": "select 1"}, None, None)])

    r = runner.invoke(app, ["verify-memory", str(golden), "-c", str(cfg)])

    assert r.exit_code == 0, r.output
    assert "PASS 1" in r.output


def test_any_changed_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q1", "SELECT id FROM users")])
    _patch_driver(
        monkeypatch,
        [("ok", [], {"sql": "SELECT id FROM accounts"}, None, None)],
    )

    r = runner.invoke(app, ["verify-memory", str(golden), "-c", str(cfg)])

    assert r.exit_code == 1, r.output
    assert "CHANGED 1" in r.output
    assert "Expected SQL" in r.output
    assert "Actual SQL" in r.output


def test_error_case_exits_nonzero_and_shows_message(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q1", "SELECT 1")])
    _patch_driver(monkeypatch, [RuntimeError("LLM down")])

    r = runner.invoke(app, ["verify-memory", str(golden), "-c", str(cfg)])

    assert r.exit_code == 1, r.output
    assert "ERROR 1" in r.output
    assert "LLM down" in r.output


def test_empty_golden_file_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    """The CLAUDE.md "structured signal, never silent success" guard."""
    cfg = _config(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text("{}")

    r = runner.invoke(app, ["verify-memory", str(empty), "-c", str(cfg)])

    assert r.exit_code == 1, r.output
    assert "empty" in r.output.lower() or "nothing to verify" in r.output.lower()


def test_empty_pairs_list_exits_nonzero(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [])

    r = runner.invoke(app, ["verify-memory", str(golden), "-c", str(cfg)])

    assert r.exit_code == 1, r.output


def test_fail_under_allows_partial_pass_rate(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path)
    golden = _golden(
        tmp_path,
        [("q1", "SELECT 1"), ("q2", "SELECT 2"), ("q3", "SELECT 3")],
    )
    _patch_driver(
        monkeypatch,
        [
            ("ok", [], {"sql": "SELECT 1"}, None, None),
            ("ok", [], {"sql": "SELECT 2"}, None, None),
            ("ok", [], {"sql": "SELECT 4"}, None, None),  # CHANGED — 2/3 pass.
        ],
    )

    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "-c", str(cfg), "--fail-under", "60"],
    )
    # 66.7% >= 60% → pass under tolerance.
    assert r.exit_code == 0, r.output


def test_fail_under_zero_rejects_all_error_run(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLAUDE.md vacuous-success guard: --fail-under 0 must not exit 0
    on an all-non-PASS run, even though 0% >= 0% reads as a "pass".
    """
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q1", "SELECT 1"), ("q2", "SELECT 2")])
    _patch_driver(
        monkeypatch,
        [
            RuntimeError("boom 1"),
            RuntimeError("boom 2"),
        ],
    )
    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "-c", str(cfg), "--fail-under", "0"],
    )
    assert r.exit_code == 1, r.output
    assert "no case PASSed" in r.output or "all-non-PASS" in r.output.lower()


def test_fail_under_zero_passes_when_any_case_passes(
    tmp_path: Path, monkeypatch
) -> None:
    """The dual: --fail-under 0 with at least one PASS exits 0."""
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q1", "SELECT 1"), ("q2", "SELECT 2")])
    _patch_driver(
        monkeypatch,
        [
            ("ok", [], {"sql": "SELECT 1"}, None, None),  # PASS
            RuntimeError("boom"),  # ERROR
        ],
    )
    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "-c", str(cfg), "--fail-under", "0"],
    )
    assert r.exit_code == 0, r.output


def test_fail_under_rejects_below_threshold(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    golden = _golden(
        tmp_path,
        [("q1", "SELECT 1"), ("q2", "SELECT 2"), ("q3", "SELECT 3")],
    )
    _patch_driver(
        monkeypatch,
        [
            ("ok", [], {"sql": "SELECT 1"}, None, None),
            ("ok", [], {"sql": "SELECT_X"}, None, None),  # CHANGED
            ("ok", [], {"sql": "SELECT_Y"}, None, None),  # CHANGED
        ],
    )

    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "-c", str(cfg), "--fail-under", "60"],
    )
    # 33% < 60% → reject.
    assert r.exit_code == 1, r.output
    assert "below threshold" in r.output


def test_csv_golden_file(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    golden = tmp_path / "golden.csv"
    golden.write_text("question,sql\nq1,SELECT 1\n")
    _patch_driver(monkeypatch, [("ok", [], {"sql": "select 1"}, None, None)])

    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "--format", "csv", "-c", str(cfg)],
    )
    assert r.exit_code == 0, r.output


def test_invalid_format_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q", "SELECT 1")])
    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "--format", "xml", "-c", str(cfg)],
    )
    assert r.exit_code == 1
    assert "json" in r.output and "csv" in r.output


def test_missing_api_key_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        f"""
[database]
url = "sqlite:///:memory:"
name = "primary"

[memory]
persist_dir = "{tmp_path / 'chroma'}"

[auth]
mode = "none"
"""
    )
    golden = _golden(tmp_path, [("q", "SELECT 1")])
    r = runner.invoke(
        app, ["verify-memory", str(golden), "-c", str(cfg_path)]
    )
    assert r.exit_code == 2


def test_missing_golden_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    r = runner.invoke(
        app, ["verify-memory", str(tmp_path / "nope.json"), "-c", str(cfg)]
    )
    assert r.exit_code == 1


@pytest.mark.parametrize("fail_under", ["-1", "101", "abc"])
def test_fail_under_invalid_value(
    tmp_path: Path, fail_under: str
) -> None:
    cfg = _config(tmp_path)
    golden = _golden(tmp_path, [("q", "SELECT 1")])
    r = runner.invoke(
        app,
        ["verify-memory", str(golden), "-c", str(cfg), "--fail-under", fail_under],
    )
    # Typer rejects out-of-range / non-numeric values with exit code 2.
    assert r.exit_code == 2
