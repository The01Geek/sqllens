# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CLI-level import-memory / export-memory round-trip with fake embeddings."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sqllens.cli import app
from tests.unit._memory_helpers import patch_fake_embeddings

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


def test_import_then_export_round_trip(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text(
        '{"sql_pairs": {"pairs": [{"question": "How many?", "sql": "SELECT 1"}]},'
        ' "schema_docs": [{"content": "users table"}]}'
    )

    r1 = runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert "saved=2" in r1.output

    r2 = runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])
    assert r2.exit_code == 0, r2.output
    assert "saved=0" in r2.output
    assert "skipped_duplicate=2" in r2.output

    out = tmp_path / "out.json"
    r3 = runner.invoke(app, ["export-memory", str(out), "-c", str(cfg)])
    assert r3.exit_code == 0, r3.output
    assert out.exists()

    r4 = runner.invoke(app, ["import-memory", str(out), "-c", str(cfg)])
    assert r4.exit_code == 0, r4.output
    assert "saved=0" in r4.output
    assert "skipped_duplicate=2" in r4.output


def test_csv_import(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    csv_file = tmp_path / "pairs.csv"
    csv_file.write_text("question,sql\nHow many users?,SELECT count(*) FROM users\n")

    r = runner.invoke(
        app, ["import-memory", str(csv_file), "--format", "csv", "-c", str(cfg)]
    )
    assert r.exit_code == 0, r.output
    assert "saved=1" in r.output


def test_dry_run_writes_nothing(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text('{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}')

    r = runner.invoke(
        app, ["import-memory", str(bundle), "--dry-run", "-c", str(cfg)]
    )
    assert r.exit_code == 0, r.output
    assert "(dry-run)" in r.output

    out = tmp_path / "out.json"
    runner.invoke(app, ["export-memory", str(out), "-c", str(cfg)])
    assert out.read_text().strip() == "{}"


def test_clear_requires_confirmation(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text('{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}')
    runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])

    declined = runner.invoke(
        app, ["import-memory", str(bundle), "--clear", "-c", str(cfg)], input="n\n"
    )
    assert declined.exit_code != 0

    confirmed = runner.invoke(
        app, ["import-memory", str(bundle), "--clear", "-c", str(cfg)], input="y\n"
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert "skipped_duplicate=0" in confirmed.output
