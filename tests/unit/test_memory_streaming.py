# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Streaming bundle reader + streaming import/export (CLI-only path).

The streaming reader is the highest-risk piece of issue #207 because the
hand-rolled framing must stay string- and escape-aware: a ``}``, ``]``, or
``"`` inside a SQL string literal must not break record boundaries. Cover
that with adversarial fixtures alongside the happy-path round-trips.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from sqllens.memory import (
    MemoryStore,
    StreamExportResult,
    StreamImportResult,
    export_bundle,
    export_bundle_stream,
    import_bundle_stream,
)
from sqllens.memory.io import BundleFormatError, parse_json
from sqllens.memory.schema import CONTENT_MAX, QUESTION_MAX, SQL_MAX
from sqllens.memory.store import schema_doc_id, sql_pair_id
from sqllens.memory.streaming import stream_records
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings


@pytest.fixture
def store(tmp_path, monkeypatch) -> MemoryStore:
    patch_fake_embeddings(monkeypatch)
    cfg = build_test_config(tmp_path / "chroma")
    return MemoryStore(cfg)


# --- Reader unit tests ----------------------------------------------------


def _stream(text: str) -> list[tuple[str, dict]]:
    return list(stream_records(io.StringIO(text)))


def test_stream_yields_one_record_at_a_time() -> None:
    text = json.dumps(
        {
            "sql_pairs": {
                "training_type": "sql_pairs",
                "pairs": [
                    {"question": "Q1", "sql": "SELECT 1"},
                    {"question": "Q2", "sql": "SELECT 2"},
                ],
            },
            "schema_docs": [{"content": "doc one"}, {"content": "doc two"}],
        }
    )
    records = _stream(text)
    assert [(k, r) for (k, r) in records] == [
        ("sql_pair", {"question": "Q1", "sql": "SELECT 1"}),
        ("sql_pair", {"question": "Q2", "sql": "SELECT 2"}),
        ("schema_doc", {"content": "doc one"}),
        ("schema_doc", {"content": "doc two"}),
    ]


def test_stream_handles_braces_inside_sql_string() -> None:
    """A `}`, `]`, or `"` inside a SQL string literal must NOT break framing.

    Without escape-aware scanning the depth tracker would close the record
    early and emit a syntactically broken fragment.
    """
    sql = "SELECT * FROM t WHERE name = '}' AND tag = ']' AND alias = '\"hi\"'"
    text = json.dumps(
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "weird Q?", "sql": sql},
                    {"question": "next Q", "sql": "SELECT 2"},
                ]
            }
        }
    )
    records = _stream(text)
    assert records == [
        ("sql_pair", {"question": "weird Q?", "sql": sql}),
        ("sql_pair", {"question": "next Q", "sql": "SELECT 2"}),
    ]


def test_stream_handles_escaped_quotes_inside_strings() -> None:
    """Backslash-escape sequences must keep the in-string flag set."""
    sql = 'SELECT \\"col\\" FROM t WHERE x = \'escaped \\\' quote\''
    text = (
        '{"sql_pairs": {"pairs": ['
        f'{{"question": "q", "sql": {json.dumps(sql)}}}'
        "]}}"
    )
    records = _stream(text)
    assert len(records) == 1
    assert records[0][1]["sql"] == sql


def test_stream_handles_unicode_multibyte_content() -> None:
    content = "schéma — 🚀 ñ"
    text = json.dumps({"schema_docs": [{"content": content}]})
    records = _stream(text)
    assert records == [("schema_doc", {"content": content})]


def test_stream_skips_unknown_top_level_keys() -> None:
    text = json.dumps(
        {
            "_meta": {"version": 1, "generated_at": "2026-01-01"},
            "sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]},
            "schema_docs": [],
        }
    )
    records = _stream(text)
    assert records == [("sql_pair", {"question": "q", "sql": "SELECT 1"})]


def test_stream_empty_object_yields_nothing() -> None:
    assert _stream("{}") == []


def test_stream_empty_input_yields_nothing() -> None:
    assert _stream("") == []
    assert _stream("   \n\n   ") == []


def test_stream_rejects_malformed_json() -> None:
    with pytest.raises(BundleFormatError):
        _stream('{"sql_pairs": {"pairs": [{"question": "q", "sql": broken')


def test_stream_rejects_non_object_record() -> None:
    with pytest.raises(BundleFormatError, match="must be a JSON object"):
        _stream('{"sql_pairs": {"pairs": ["not an object"]}}')


def test_stream_rejects_unbalanced_braces() -> None:
    with pytest.raises(BundleFormatError):
        _stream('{"sql_pairs": {"pairs": [{"q": "x", "sql": "SELECT 1"')


def test_stream_handles_large_chunk_boundaries(tmp_path) -> None:
    """Records that straddle the default chunked read boundary must reassemble.

    300 pairs of ~120 bytes each well exceeds the 64 KiB default chunk, so
    the depth-tracked refill is exercised across many element boundaries.
    """
    pairs = [
        {"question": f"q{i}", "sql": "SELECT " + "x" * 100 + f" -- pair{i}"}
        for i in range(300)
    ]
    text = json.dumps({"sql_pairs": {"pairs": pairs}})
    fp = io.StringIO(text)
    records = list(stream_records(fp))
    assert len(records) == 300
    assert records[0] == ("sql_pair", pairs[0])
    assert records[-1] == ("sql_pair", pairs[-1])


# --- Streaming import (against a real store) ------------------------------


def _write(tmp_path: Path, payload: dict, name: str = "in.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


async def test_stream_import_basic(store: MemoryStore, tmp_path) -> None:
    path = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "Q?", "sql": "SELECT 1"},
                    {"question": "R?", "sql": "SELECT 2"},
                ]
            },
            "schema_docs": [{"content": "doc"}],
        },
    )
    result = await import_bundle_stream(store, path)
    assert isinstance(result, StreamImportResult)
    assert result.report.saved == 3
    assert result.report.errors == []
    assert result.aborted is False
    assert result.bytes_read == path.stat().st_size


async def test_stream_import_per_item_caps_apply(
    store: MemoryStore, tmp_path
) -> None:
    """Per-item caps (``QUESTION_MAX`` / ``SQL_MAX`` / ``CONTENT_MAX``) still
    fire on the streaming path — only the whole-file caps are lifted."""
    over = "q" * (QUESTION_MAX + 1)
    over_sql = "s" * (SQL_MAX + 1)
    over_content = "c" * (CONTENT_MAX + 1)
    path = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": over, "sql": "SELECT 1"},
                    {"question": "ok", "sql": over_sql},
                    {"question": "fine", "sql": "SELECT 3"},
                ]
            },
            "schema_docs": [
                {"content": over_content},
                {"content": "real content"},
            ],
        },
    )
    result = await import_bundle_stream(store, path)
    assert result.report.saved == 2  # one valid pair + one valid doc
    assert len(result.report.errors) == 3
    assert {e.kind for e in result.report.errors} == {"sql_pair", "schema_doc"}


async def test_stream_import_is_idempotent_upsert(
    store: MemoryStore, tmp_path
) -> None:
    path = _write(
        tmp_path,
        {
            "sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]},
            "schema_docs": [{"content": "c"}],
        },
    )
    await import_bundle_stream(store, path)
    await import_bundle_stream(store, path)
    final = store.iter_all()
    assert final.sql_pairs is not None
    assert len(final.sql_pairs.pairs) == 1
    assert final.schema_docs is not None
    assert len(final.schema_docs) == 1


async def test_stream_import_intra_batch_id_collision_collapses(
    store: MemoryStore, tmp_path
) -> None:
    """Two records in ONE batch that normalize to the same content-hash id
    must collapse to a single ``upsert`` row, not crash the whole batch with
    ChromaDB's ``DuplicateIDError``.

    Regression for the PR #209 review finding: before the fix, a batch
    containing ``ids=[dup, dup]`` raised ``DuplicateIDError``, which the
    per-batch ``except`` flattened into "every item in the batch errored"
    (``saved == 0``) — and under ``--clear`` could leave the store
    wiped-and-empty. ``"How many?"/"SELECT 1"`` and ``"how   MANY?"/"select
    1"`` normalize identically (whitespace-collapsed, lowercased).
    """
    path = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "How many?", "sql": "SELECT 1"},
                    {"question": "how   MANY?", "sql": "select 1"},
                ]
            },
            "schema_docs": [
                {"content": "Status is open."},
                {"content": "status   is OPEN."},
            ],
        },
    )
    # Large batch_size so both colliding records land in the SAME batch.
    result = await import_bundle_stream(store, path, batch_size=100)

    # The batch did not error out: no DuplicateIDError surfaced.
    assert result.report.errors == []
    assert result.aborted is False
    # ``saved`` counts records processed (consistent with the bounded path's
    # per-item counting), so both colliding records count even though they
    # collapse to one stored row.
    assert result.report.saved == 4

    # The store holds exactly one row per collision group (last write wins).
    final = store.iter_all()
    assert final.sql_pairs is not None
    assert len(final.sql_pairs.pairs) == 1
    assert final.schema_docs is not None
    assert len(final.schema_docs) == 1


async def test_stream_import_id_scheme_matches_sql_pair_id_helper(
    store: MemoryStore, tmp_path
) -> None:
    """The Chroma row id must be the documented content-hash, not a uuid."""
    path = _write(
        tmp_path,
        {"sql_pairs": {"pairs": [{"question": "How many?", "sql": "SELECT 1"}]}},
    )
    await import_bundle_stream(store, path)
    collection = store._mem._get_collection()
    ids = collection.get()["ids"]
    assert sql_pair_id("How many?", "SELECT 1") in ids


async def test_stream_import_aborted_on_malformed_input(
    store: MemoryStore, tmp_path
) -> None:
    """A framing error mid-file aborts cleanly with counts so far + non-empty
    abort_reason; partial writes already in the store remain (no rollback)."""
    path = tmp_path / "bad.json"
    path.write_text(
        '{"sql_pairs": {"pairs": ['
        '{"question": "ok", "sql": "SELECT 1"},'
        '{"question": "broken'  # missing closing quote -> framing error
    )
    result = await import_bundle_stream(store, path)
    assert result.aborted is True
    assert result.abort_reason
    # Pre-failure record landed.
    assert result.report.saved == 1


async def test_stream_import_clear_then_failure_leaves_partial(
    store: MemoryStore, tmp_path
) -> None:
    """``--clear`` followed by a failed stream-import is the documented
    data-loss path — the partial state is observable on the store."""
    seed = _write(
        tmp_path,
        {"sql_pairs": {"pairs": [{"question": "seed", "sql": "SELECT 0"}]}},
        name="seed.json",
    )
    await import_bundle_stream(store, seed)
    assert store.iter_all().sql_pairs is not None

    broken = tmp_path / "broken.json"
    broken.write_text(
        '{"sql_pairs": {"pairs": [{"question": "', encoding="utf-8"
    )
    result = await import_bundle_stream(store, broken, clear=True)
    assert result.aborted is True
    # Clear ran, broken file yielded zero — store is now empty.
    final = store.iter_all()
    assert final.sql_pairs is None
    assert final.schema_docs is None


# --- Streaming export (against a real store) ------------------------------


async def test_stream_export_round_trip_via_bounded_parser(
    store: MemoryStore, tmp_path
) -> None:
    """Streamed export must round-trip through the bounded parse_json — the
    streamed bytes are valid bundle JSON."""
    seed = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "Q?", "sql": "SELECT 1"},
                    {"question": "R?", "sql": "SELECT 2"},
                ]
            },
            "schema_docs": [{"content": "doc one"}, {"content": "doc two"}],
        },
    )
    await import_bundle_stream(store, seed)

    out = tmp_path / "out.json"
    result = export_bundle_stream(store, out)
    assert isinstance(result, StreamExportResult)
    assert result.sql_pairs_count == 2
    assert result.schema_docs_count == 2
    assert result.skipped_rows == 0

    reparsed = parse_json(out.read_text())
    assert reparsed.sql_pairs is not None
    assert {p.sql for p in reparsed.sql_pairs.pairs} == {"SELECT 1", "SELECT 2"}
    assert reparsed.schema_docs is not None
    assert {d.content for d in reparsed.schema_docs} == {"doc one", "doc two"}


async def test_stream_export_round_trip_via_streaming_reader(
    store: MemoryStore, tmp_path
) -> None:
    seed = _write(
        tmp_path,
        {
            "sql_pairs": {"pairs": [{"question": "Q?", "sql": "SELECT 1"}]},
            "schema_docs": [{"content": "doc"}],
        },
    )
    await import_bundle_stream(store, seed)

    out = tmp_path / "out.json"
    export_bundle_stream(store, out)

    with out.open() as fp:
        records = list(stream_records(fp))
    kinds = [k for k, _ in records]
    assert kinds == ["sql_pair", "schema_doc"]


async def test_stream_export_empty_store_emits_warning(
    store: MemoryStore, tmp_path
) -> None:
    out = tmp_path / "out.json"
    result = export_bundle_stream(store, out)
    assert result.sql_pairs_count == 0
    assert result.schema_docs_count == 0
    assert any("empty" in w for w in result.warnings)
    # The file is still written; it parses as a valid (empty) bundle.
    reparsed = parse_json(out.read_text())
    assert reparsed.sql_pairs is None or not reparsed.sql_pairs.pairs


async def test_stream_export_consistent_with_bounded_export(
    store: MemoryStore, tmp_path
) -> None:
    """Streamed export and bounded export must produce equivalent bundles."""
    seed = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "qa", "sql": "SELECT 1"},
                    {"question": "qb", "sql": "SELECT 2"},
                ]
            },
            "schema_docs": [{"content": "doc"}],
        },
    )
    await import_bundle_stream(store, seed)

    streamed = tmp_path / "streamed.json"
    export_bundle_stream(store, streamed)
    bounded = export_bundle(store, "json").text

    streamed_bundle = parse_json(streamed.read_text())
    bounded_bundle = parse_json(bounded)
    assert streamed_bundle == bounded_bundle


def test_schema_doc_id_matches_documented_scheme() -> None:
    """Pin the AC contract: ``sha256("schema_doc\\x00" + _norm(content))``."""
    import hashlib

    expected = hashlib.sha256(b"schema_doc\x00hello world").hexdigest()
    assert schema_doc_id("Hello   World") == expected


async def test_stream_import_per_batch_upsert_failure(
    store: MemoryStore, tmp_path, monkeypatch
) -> None:
    """A ``RuntimeError`` from ``add_sql_pair_batch`` must mark every member
    of the batch as errored — never silently advance ``saved``. Covers the
    batched-write failure path that bounded ``import_bundle`` already tests
    on the per-item path."""
    path = _write(
        tmp_path,
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "q0", "sql": "SELECT 0"},
                    {"question": "q1", "sql": "SELECT 1"},
                    {"question": "q2", "sql": "SELECT 2"},
                ]
            }
        },
    )

    async def flaky_batch(items: list[tuple[str, str]]) -> None:
        raise RuntimeError("batch-boom")

    monkeypatch.setattr(store, "add_sql_pair_batch", flaky_batch)
    result = await import_bundle_stream(store, path, batch_size=2)
    assert result.report.saved == 0
    assert len(result.report.errors) == 3
    assert all(e.kind == "sql_pair" for e in result.report.errors)
    assert {e.index for e in result.report.errors} == {0, 1, 2}
    assert all("batch-boom" in e.message for e in result.report.errors)


async def test_stream_export_paginates_across_pages(
    store: MemoryStore, tmp_path
) -> None:
    """Multi-page iteration: seed more rows than ``page_size``, then assert
    the streamed export captures every row. Without this test a regression
    that returned only the first page would silently truncate large
    exports."""
    pairs = [{"question": f"q{i}", "sql": f"SELECT {i}"} for i in range(7)]
    docs = [{"content": f"doc {i}"} for i in range(5)]
    seed = _write(tmp_path, {"sql_pairs": {"pairs": pairs}, "schema_docs": docs})
    await import_bundle_stream(store, seed)

    out = tmp_path / "out.json"
    result = export_bundle_stream(store, out, page_size=2)
    assert result.sql_pairs_count == 7
    assert result.schema_docs_count == 5

    reparsed = parse_json(out.read_text())
    assert reparsed.sql_pairs is not None
    assert {p.sql for p in reparsed.sql_pairs.pairs} == {f"SELECT {i}" for i in range(7)}
    assert reparsed.schema_docs is not None
    assert {d.content for d in reparsed.schema_docs} == {f"doc {i}" for i in range(5)}


async def test_stream_export_atomic_write_preserves_existing_on_failure(
    store: MemoryStore, tmp_path, monkeypatch
) -> None:
    """A mid-stream OSError must NOT destroy a previously-good backup at
    the destination path. The streaming exporter writes to a sibling
    ``.tmp`` and ``os.replace``s only on full success — so a disk-full
    mid-write leaves the prior file intact, satisfying CLAUDE.md's
    'export before --clear' invariant."""
    # First, produce a valid backup file.
    seed = _write(
        tmp_path,
        {"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}},
    )
    await import_bundle_stream(store, seed)
    backup = tmp_path / "backup.json"
    export_bundle_stream(store, backup)
    good_bytes = backup.read_text()
    assert "SELECT 1" in good_bytes

    # Now simulate a mid-export failure on a second export to the same path.
    real_paginated = store.iter_paginated

    def flaky_iter(*, where=None, page_size=500):
        for kind, model in real_paginated(where=where, page_size=page_size):
            yield kind, model
            raise OSError("disk full")

    monkeypatch.setattr(store, "iter_paginated", flaky_iter)
    with pytest.raises(OSError, match="disk full"):
        export_bundle_stream(store, backup)

    # The prior good backup must still be on disk, byte-identical.
    assert backup.read_text() == good_bytes
    # No stray .tmp file left behind.
    assert not (tmp_path / "backup.json.tmp").exists()


async def test_stream_import_dry_run_does_not_clear(
    store: MemoryStore, tmp_path
) -> None:
    """``--dry-run --clear`` must NOT wipe the store; per the documented
    contract dry-run is a no-op against the live store. Without this guard,
    a regression that ran ``clear()`` on the dry-run path would silently
    destroy data on what the operator believes is a preview."""
    seed = _write(
        tmp_path,
        {"sql_pairs": {"pairs": [{"question": "seed", "sql": "SELECT 0"}]}},
    )
    await import_bundle_stream(store, seed)
    before = store.iter_all()
    assert before.sql_pairs is not None

    other = _write(
        tmp_path,
        {"sql_pairs": {"pairs": [{"question": "new", "sql": "SELECT 1"}]}},
        name="other.json",
    )
    result = await import_bundle_stream(store, other, dry_run=True, clear=True)
    assert result.report.saved == 1
    assert result.report.errors == []
    # Store is unchanged: dry-run + clear must be a no-op.
    after = store.iter_all()
    assert after.sql_pairs is not None
    assert {p.sql for p in after.sql_pairs.pairs} == {"SELECT 0"}


async def test_stream_import_bypasses_whole_file_byte_cap(
    store: MemoryStore, tmp_path
) -> None:
    """The headline feature: ``--stream`` lifts ``MAX_BUNDLE_BYTES`` for the
    local operator. A bundle whose serialized byte length exceeds the
    bounded path's 10 MiB cap must import successfully via streaming."""
    from sqllens.memory.schema import MAX_BUNDLE_BYTES

    # Construct a bundle whose UTF-8 size is comfortably above MAX_BUNDLE_BYTES.
    # Each pair is roughly the per-item cap (~10 KB SQL), so a few thousand
    # pairs push past 10 MiB without any single record exceeding per-item caps.
    big_sql = "SELECT " + ("x" * 9000) + " -- pad"
    pairs = [{"question": f"q{i}", "sql": big_sql} for i in range(1200)]
    big_bundle = tmp_path / "big.json"
    big_bundle.write_text(json.dumps({"sql_pairs": {"pairs": pairs}}))
    assert big_bundle.stat().st_size > MAX_BUNDLE_BYTES, "bundle must exceed bounded cap"

    result = await import_bundle_stream(store, big_bundle, batch_size=200)
    # Idempotent upsert: all rows normalize to distinct sql_pair_ids because
    # the question differs per row. saved counts the upserts.
    assert result.report.saved == 1200
    assert result.report.errors == []
    assert result.aborted is False
    assert result.likely_format_mismatch is False


def test_stream_reader_eof_inside_string_literal_raises() -> None:
    """An unterminated string literal must raise BundleFormatError, not
    hang or grow the buffer without bound. The outer ``_scan_balanced``
    catches the EOF first when the unterminated string sits inside a
    record object, but the failure mode is the same: a clean structured
    error rather than a hang."""
    with pytest.raises(BundleFormatError, match="EOF inside"):
        _stream('{"sql_pairs": {"pairs": [{"question": "unterminated')


def test_stream_reader_per_value_byte_cap_fires_on_oversized_string() -> None:
    """The 1 MiB per-value cap inside ``_scan_string`` must fire when a
    single value would exceed the cap — defence-in-depth against an
    adversarial unterminated literal."""
    from sqllens.memory.streaming import _MAX_VALUE_BYTES

    # Build an unterminated string just past the cap. The scanner must raise
    # the cap-exceeded error rather than running until EOF (which would
    # require the whole 1 MiB+ to be in the buffer).
    oversized = '{"sql_pairs": {"pairs": [{"question": "' + ("x" * (_MAX_VALUE_BYTES + 8))
    with pytest.raises(BundleFormatError, match="exceeds"):
        _stream(oversized)


def test_sql_pair_id_matches_documented_scheme() -> None:
    """Pin the AC contract: ``sha256("sql_pair\\x00" + _norm(q) + "\\x00" + _norm(sql))``."""
    import hashlib

    expected = hashlib.sha256(
        b"sql_pair\x00how many users?\x00select count(*) from users"
    ).hexdigest()
    assert (
        sql_pair_id(" How  MANY users? ", "select COUNT(*) from users") == expected
    )
