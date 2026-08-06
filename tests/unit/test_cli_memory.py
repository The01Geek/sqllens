# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CLI-level import-memory / export-memory round-trip with fake embeddings."""

from __future__ import annotations

import json
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
        '{"sql_pairs": [{"question": "How many?", "sql": "SELECT 1"}],'
        ' "schema_docs": [{"content": "users table"}]}'
    )

    r1 = runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert "saved=2" in r1.output
    assert "errors=0" in r1.output

    # Re-import is idempotent: ``saved`` counts upserts (every row was
    # written again under the same content-hash id), and no separate
    # ``skipped_duplicate`` count is emitted.
    r2 = runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])
    assert r2.exit_code == 0, r2.output
    assert "saved=2" in r2.output
    assert "errors=0" in r2.output
    assert "skipped_duplicate" not in r2.output

    out = tmp_path / "out.json"
    r3 = runner.invoke(app, ["export-memory", str(out), "-c", str(cfg)])
    assert r3.exit_code == 0, r3.output
    assert out.exists()

    r4 = runner.invoke(app, ["import-memory", str(out), "-c", str(cfg)])
    assert r4.exit_code == 0, r4.output
    assert "saved=2" in r4.output


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
    bundle.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 1"}]}')

    r = runner.invoke(
        app, ["import-memory", str(bundle), "--dry-run", "-c", str(cfg)]
    )
    assert r.exit_code == 0, r.output
    assert "(dry-run)" in r.output

    out = tmp_path / "out.json"
    runner.invoke(app, ["export-memory", str(out), "-c", str(cfg)])
    assert out.read_text().strip() == "{}"


def test_clear_then_import_failure_warns_data_loss(tmp_path, monkeypatch) -> None:
    """The most consequential message in the PR: --clear wiped, import died."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 1"}]}')
    runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])

    async def boom(*args, **kwargs):
        raise RuntimeError("write exploded")

    monkeypatch.setattr("sqllens.memory.import_bundle", boom)
    monkeypatch.setattr("sqllens.cli.import_bundle", boom, raising=False)

    r = runner.invoke(
        app, ["import-memory", str(bundle), "--clear", "-c", str(cfg)], input="y\n"
    )
    assert r.exit_code == 1, r.output
    assert "wiped" in r.output
    assert "empty or partial" in r.output


def test_export_empty_store_warns(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    out = tmp_path / "out.json"
    r = runner.invoke(app, ["export-memory", str(out), "-c", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "empty" in r.output
    assert out.exists()


def test_stream_import_then_export_round_trip(tmp_path, monkeypatch) -> None:
    """``--stream`` works on both directions; bytes_read is surfaced; output
    file from a streaming export re-imports cleanly via the streaming path."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text(
        '{"sql_pairs": [{"question": "Q?", "sql": "SELECT 1"}],'
        ' "schema_docs": [{"content": "doc"}]}'
    )

    r1 = runner.invoke(
        app, ["import-memory", str(bundle), "--stream", "-c", str(cfg)]
    )
    assert r1.exit_code == 0, r1.output
    assert "saved=2" in r1.output
    assert "failed=0" in r1.output
    assert "bytes_read=" in r1.output

    out = tmp_path / "out.json"
    r2 = runner.invoke(
        app, ["export-memory", str(out), "--stream", "-c", str(cfg)]
    )
    assert r2.exit_code == 0, r2.output
    assert out.exists()

    r3 = runner.invoke(
        app, ["import-memory", str(out), "--stream", "-c", str(cfg)]
    )
    assert r3.exit_code == 0, r3.output
    assert "saved=2" in r3.output


def test_stream_import_rejects_csv_format(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.csv"
    bundle.write_text("question,sql\nq,SELECT 1\n")
    r = runner.invoke(
        app,
        [
            "import-memory",
            str(bundle),
            "--stream",
            "--format",
            "csv",
            "-c",
            str(cfg),
        ],
    )
    assert r.exit_code == 1, r.output
    assert "JSON-only" in r.output or "JSON only" in r.output


def test_stream_import_loud_warning_on_zero_records(tmp_path, monkeypatch) -> None:
    """A non-trivial input that yields zero records is almost always a
    format mismatch — surface loudly and exit non-zero, per CLAUDE.md's
    'lossy/empty success needs a loud warning' rule."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text(
        '{"meta": {"version": 1, "note": "this is not a bundle"}}'
        + (" " * 100)  # pad past the empty-input threshold
    )
    r = runner.invoke(
        app, ["import-memory", str(bundle), "--stream", "-c", str(cfg)]
    )
    assert r.exit_code == 1, r.output
    assert "Warning" in r.output
    assert "0 records" in r.output


def test_stream_import_exits_non_zero_on_per_item_failure(
    tmp_path, monkeypatch
) -> None:
    """Per-item validation failure on the streaming path is a non-zero exit."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    from sqllens.memory.schema import QUESTION_MAX

    bundle = tmp_path / "in.json"
    over = "q" * (QUESTION_MAX + 1)
    bundle.write_text(
        '{"sql_pairs": ['
        f'{{"question": {json.dumps(over)}, "sql": "SELECT 1"}},'
        '{"question": "ok", "sql": "SELECT 2"}'
        "]}"
    )
    r = runner.invoke(
        app, ["import-memory", str(bundle), "--stream", "-c", str(cfg)]
    )
    assert r.exit_code == 1, r.output
    assert "failed=1" in r.output


def test_stream_import_clear_then_failure_warns_data_loss(
    tmp_path, monkeypatch
) -> None:
    """``--clear`` + a stream-import that fails partway is the documented
    data-loss path — must exit non-zero AND surface the wiped warning."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)

    # Seed with one record so --clear has something to wipe.
    seed = tmp_path / "seed.json"
    seed.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 0"}]}')
    runner.invoke(app, ["import-memory", str(seed), "-c", str(cfg)])

    broken = tmp_path / "broken.json"
    broken.write_text('{"sql_pairs": [{"question": "', encoding="utf-8")
    r = runner.invoke(
        app,
        [
            "import-memory",
            str(broken),
            "--stream",
            "--clear",
            "-c",
            str(cfg),
        ],
        input="y\n",
    )
    assert r.exit_code == 1, r.output
    assert "aborted" in r.output.lower()
    assert "wiped" in r.output.lower() or "may now be empty" in r.output.lower()


def test_stream_clear_with_empty_input_warns_and_exits_nonzero(
    tmp_path, monkeypatch
) -> None:
    """``--clear`` + an empty (or near-empty, sub-threshold) input file is the
    documented data-loss trap: the operator typed ``--clear`` expecting
    wipe-and-replace but got wipe-and-empty. The CLI must surface a loud
    warning and exit non-zero — never green output on a wiped store."""
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    seed = tmp_path / "seed.json"
    seed.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 0"}]}')
    runner.invoke(app, ["import-memory", str(seed), "-c", str(cfg)])

    empty = tmp_path / "empty.json"
    empty.write_text("{}")  # bundle-shaped but legitimately empty
    r = runner.invoke(
        app,
        ["import-memory", str(empty), "--stream", "--clear", "-c", str(cfg)],
        input="y\n",
    )
    assert r.exit_code == 1, r.output
    assert "Warning" in r.output
    assert "wiped" in r.output.lower() or "now empty" in r.output.lower()


def test_stream_export_rejects_csv_format(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    out = tmp_path / "out.csv"
    r = runner.invoke(
        app,
        [
            "export-memory",
            str(out),
            "--stream",
            "--format",
            "csv",
            "-c",
            str(cfg),
        ],
    )
    assert r.exit_code == 1, r.output


def test_clear_requires_confirmation(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    bundle = tmp_path / "in.json"
    bundle.write_text('{"sql_pairs": [{"question": "q", "sql": "SELECT 1"}]}')
    runner.invoke(app, ["import-memory", str(bundle), "-c", str(cfg)])

    declined = runner.invoke(
        app, ["import-memory", str(bundle), "--clear", "-c", str(cfg)], input="n\n"
    )
    assert declined.exit_code != 0

    confirmed = runner.invoke(
        app, ["import-memory", str(bundle), "--clear", "-c", str(cfg)], input="y\n"
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert "saved=1" in confirmed.output
    assert "errors=0" in confirmed.output
