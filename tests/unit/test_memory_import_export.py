# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Import/export against a real ChromaAgentMemory with fake embeddings."""

from __future__ import annotations

import pytest

from sqllens.memory import MemoryStore, export_bundle, import_bundle
from sqllens.memory.io import parse_json
from sqllens.memory.schema import MemoryBundle
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings


@pytest.fixture
def store(tmp_path, monkeypatch) -> MemoryStore:
    patch_fake_embeddings(monkeypatch)
    cfg = build_test_config(tmp_path / "chroma")
    return MemoryStore(cfg)


_BUNDLE = MemoryBundle.model_validate(
    {
        "sql_pairs": {
            "pairs": [
                {"question": "How many users?", "sql": "SELECT count(*) FROM users"},
                {"question": "Active count", "sql": "SELECT count(*) FROM u WHERE active"},
            ]
        },
        "schema_docs": [{"content": "Table users: one row per account."}],
    }
)


async def test_import_then_reimport_skips_all_duplicates(store: MemoryStore) -> None:
    first = await import_bundle(store, _BUNDLE)
    assert first.saved == 3
    assert first.skipped_duplicate == 0
    assert first.errors == []

    second = await import_bundle(store, _BUNDLE)
    assert second.saved == 0
    assert second.skipped_duplicate == 3


async def test_intra_batch_duplicate_skipped(store: MemoryStore) -> None:
    dup = MemoryBundle.model_validate(
        {
            "sql_pairs": {
                "pairs": [
                    {"question": " How  MANY users? ", "sql": "select COUNT(*) from users"},
                    {"question": "How many users?", "sql": "SELECT count(*) FROM users"},
                ]
            }
        }
    )
    report = await import_bundle(store, dup)
    assert report.saved == 1
    assert report.skipped_duplicate == 1


async def test_round_trip_lossless(store: MemoryStore) -> None:
    await import_bundle(store, _BUNDLE)
    exported = export_bundle(store, "json")
    reparsed = parse_json(exported)

    fresh_store = store
    again = await import_bundle(fresh_store, reparsed)
    assert again.saved == 0
    assert again.skipped_duplicate == 3

    assert reparsed.sql_pairs is not None
    assert {p.sql for p in reparsed.sql_pairs.pairs} == {
        "SELECT count(*) FROM users",
        "SELECT count(*) FROM u WHERE active",
    }
    assert reparsed.schema_docs is not None
    assert reparsed.schema_docs[0].content == "Table users: one row per account."


async def test_imported_pair_stored_with_run_sql_shape(
    store: MemoryStore,
) -> None:
    """Retrieval at query time matches only if tool_name == 'run_sql'."""
    from sqllens.memory.store import RUN_SQL_TOOL_NAME

    await import_bundle(store, _BUNDLE)
    collection = store._mem._get_collection()
    metas = collection.get()["metadatas"]
    tool_metas = [m for m in metas if not m.get("is_text_memory")]
    assert tool_metas
    for meta in tool_metas:
        assert meta["tool_name"] == RUN_SQL_TOOL_NAME
        assert '"sql"' in meta["args_json"]


async def test_dry_run_writes_nothing(store: MemoryStore) -> None:
    before = export_bundle(store, "json")
    report = await import_bundle(store, _BUNDLE, dry_run=True)
    assert report.saved == 3
    assert export_bundle(store, "json") == before
    after = store.iter_all()
    assert after.sql_pairs is None
    assert after.schema_docs is None


async def test_dry_run_with_clear_preserves_store(store: MemoryStore) -> None:
    await import_bundle(store, _BUNDLE)
    before = export_bundle(store, "json")
    report = await import_bundle(store, _BUNDLE, dry_run=True, clear=True)
    # clear is skipped on a dry-run, so existing memory is still the baseline
    assert report.saved == 0
    assert report.skipped_duplicate == 3
    assert export_bundle(store, "json") == before


async def test_iter_all_skips_unrepresentable_and_corrupt_rows(
    store: MemoryStore,
) -> None:
    await import_bundle(store, _BUNDLE)
    collection = store._mem._get_collection()
    # A non-run_sql tool memory and a run_sql memory with corrupt args_json —
    # both must be skipped, not crash export / dedup-seeding.
    collection.upsert(
        ids=["other-tool", "corrupt-args"],
        documents=["q1", "q2"],
        metadatas=[
            {"question": "q1", "tool_name": "some_other_tool", "args_json": "{}"},
            {"question": "q2", "tool_name": "run_sql", "args_json": "{not json"},
        ],
    )
    bundle = store.iter_all()
    assert bundle.sql_pairs is not None
    assert {p.sql for p in bundle.sql_pairs.pairs} == {
        "SELECT count(*) FROM users",
        "SELECT count(*) FROM u WHERE active",
    }


async def test_per_item_save_failure_is_reported_not_fatal(
    store: MemoryStore, monkeypatch
) -> None:
    calls = {"n": 0}

    real_add = store.add_sql_pair

    async def flaky_add(question: str, sql: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real_add(question, sql)

    monkeypatch.setattr(store, "add_sql_pair", flaky_add)
    report = await import_bundle(store, _BUNDLE)
    assert report.saved == 2  # 1 pair failed, 1 pair + 1 doc saved
    assert len(report.errors) == 1
    assert report.errors[0].kind == "sql_pair"
    assert "boom" in report.errors[0].message


async def test_clear_wipes_first(store: MemoryStore) -> None:
    await import_bundle(store, _BUNDLE)
    assert store.iter_all().sql_pairs is not None

    other = MemoryBundle.model_validate(
        {"sql_pairs": {"pairs": [{"question": "new q", "sql": "SELECT 2"}]}}
    )
    report = await import_bundle(store, other, clear=True)
    assert report.saved == 1
    assert report.skipped_duplicate == 0

    remaining = store.iter_all()
    assert remaining.sql_pairs is not None
    assert len(remaining.sql_pairs.pairs) == 1
    assert remaining.sql_pairs.pairs[0].sql == "SELECT 2"
    assert remaining.schema_docs is None
