# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Real backend pagination and concurrent managed-write consistency."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.memory import admin
from sqllens.memory.store import MemoryStore
from tests.unit._memory_helpers import patch_fake_embeddings


@pytest.fixture
def populated_store(tmp_path, monkeypatch):
    patch_fake_embeddings(monkeypatch)
    memory = ChromaAgentMemory(str(tmp_path / "memory"), "listing_consistency")
    collection = memory._get_collection()
    ids = [f"id-{i:04}" for i in range(513)]
    metadata = [
        {"timestamp": f"{i:06}", "is_text_memory": bool(i % 2),
         "content": f"note {i}", "question": f"q{i}", "tool_name": "run_sql",
         "args_json": '{"sql":"SELECT 1"}', "success": True}
        for i in range(513)
    ]
    collection.add(ids=ids, embeddings=[[float(i)] * 16 for i in range(513)],
                   metadatas=metadata)
    store = object.__new__(MemoryStore)
    store._mem = memory
    store._ctx = None
    yield store, collection, ids, metadata
    memory._executor.shutdown(wait=True)
    memory._client._system.stop()


def test_real_chroma_pages_keep_ids_metadata_embeddings_and_filtered_order(populated_store):
    store, collection, ids, metadata = populated_store
    pages = [collection.get(limit=256, offset=i, include=["metadatas"])
             for i in (0, 256, 512, 513)]
    assert [len(page["ids"]) for page in pages] == [256, 256, 1, 0]
    records = list(store.iter_records(include_embeddings=True))
    assert [record.memory_id for record in records] == ids
    assert [record.metadata for record in records] == metadata
    assert [record.embedding for record in records] == [[float(i)] * 16 for i in range(513)]
    listed = admin.list_memories(store, memory_type="text", limit=3,
                                 include_embedding_preview=True)
    assert listed["total"] == 256
    assert [row["memory_id"] for row in listed["memories"]] == ids[511:505:-2]
    assert listed["memories"][0]["embedding_preview"] == [511.0] * 8
    assert admin.list_memories(store, limit=0) == {"memories": [], "total": 513}


async def test_background_agent_delete_refuses_partial_listing(populated_store):
    store, collection, _, _ = populated_store
    writer = ChromaAgentMemory(store._mem.persist_directory, store._mem.collection_name)
    writer_started, first_page, deleted = (threading.Event() for _ in range(3))

    def writer_get(**kwargs):
        writer_started.set()
        assert first_page.wait(10)
        return collection.get(**kwargs)

    def writer_delete(**kwargs):
        try:
            return collection.delete(**kwargs)
        finally:
            deleted.set()

    def reader_get(**kwargs):
        page = collection.get(**kwargs)
        if kwargs.get("offset") == 0:
            first_page.set()
            assert deleted.wait(10)
        return page

    writer._collection = SimpleNamespace(get=writer_get, delete=writer_delete)
    store._mem._collection = SimpleNamespace(get=reader_get)
    task = asyncio.create_task(writer.delete_by_id(None, "id-0001"))
    try:
        for _ in range(1000):
            if writer_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert writer_started.is_set()
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store, memory_type="text")
        assert await task is True
        store._mem._collection = collection
        stable = admin.list_memories(store, memory_type="text")
        assert stable["total"] == 255
        assert "id-0001" not in {row["memory_id"] for row in stable["memories"]}
    finally:
        first_page.set()
        await task
        writer._executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["delete", "clear", "replace", "failed_delete"])
def test_direct_store_writes_cannot_escape_by_balancing_count_or_raising(
    populated_store, operation
):
    store, collection, _, _ = populated_store
    writer = object.__new__(MemoryStore)
    writer._mem = ChromaAgentMemory(store._mem.persist_directory, store._mem.collection_name)
    writer._mem._collection = collection

    def get(**kwargs):
        page = collection.get(**kwargs)
        if kwargs.get("offset") == 0:
            if operation == "clear":
                writer.clear()
            elif operation == "failed_delete":
                def failing_delete(**kwargs):
                    collection.delete(**kwargs)
                    raise RuntimeError("private-storage-detail")
                writer._mem._collection = SimpleNamespace(get=collection.get, delete=failing_delete)
                with pytest.raises(RuntimeError, match="private-storage-detail"):
                    writer.delete_ids(["id-0001"])
            else:
                writer.delete_ids(["id-0001"])
                if operation == "replace":
                    asyncio.run(writer._mem.save_text_memory("replacement", None))
                    assert collection.count() == 513
        return page

    store._mem._collection = SimpleNamespace(get=get)
    try:
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store)
    finally:
        writer._mem._executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", [
    "tool_save", "text_save", "tool_delete", "text_delete", "clear", "hit_update",
])
def test_every_agent_writer_invalidates_a_scan(populated_store, operation):
    store, collection, _, _ = populated_store
    writer = ChromaAgentMemory(store._mem.persist_directory, store._mem.collection_name)
    writer._collection = collection

    async def mutate():
        if operation == "tool_save":
            await writer.save_tool_usage("new question", "run_sql", {"sql": "SELECT 1"}, None)
        elif operation == "text_save":
            await writer.save_text_memory("new note", None)
        elif operation == "tool_delete":
            assert await writer.delete_by_id(None, "id-0000")
        elif operation == "text_delete":
            assert await writer.delete_text_memory(None, "id-0001")
        elif operation == "clear":
            assert await writer.clear_memories(None) == 513
        else:
            hits = await writer.search_similar_usage("query", None, limit=1, similarity_threshold=0)
            assert len(hits) == 1
            updated = collection.get(ids=[hits[0].memory.memory_id], include=["metadatas"])
            assert updated["metadatas"][0]["hit_count"] == 1

    def get(**kwargs):
        page = collection.get(**kwargs)
        if kwargs.get("offset") == 0:
            asyncio.run(mutate())
        return page

    store._mem._collection = SimpleNamespace(get=get)
    try:
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store)
    finally:
        writer._executor.shutdown(wait=True)


def test_final_yield_is_checked_before_scan_completes(populated_store):
    store, _, _, _ = populated_store
    records = store.iter_records()
    for _ in range(513):
        next(records)
    store.delete_ids(["id-0001"])
    with pytest.raises(RuntimeError, match=r"changed.*retry"):
        next(records)


def test_equivalent_paths_share_state_but_other_collections_do_not(tmp_path):
    from sqllens._memory_consistency import collection_state

    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    state = collection_state(str(actual), "one")
    same = collection_state(str(alias / "."), "one")
    other = collection_state(str(actual), "two")
    assert state is same
    generation = state.snapshot()
    with other.writing():
        state.check(generation)
    state.check(generation)
    with same.writing():
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            state.snapshot()
    with pytest.raises(RuntimeError, match=r"changed.*retry"):
        state.check(generation)


async def test_active_failed_writer_refuses_scan_then_releases_state(populated_store):
    store, collection, _, _ = populated_store
    writer = ChromaAgentMemory(store._mem.persist_directory, store._mem.collection_name)
    entered, release = threading.Event(), threading.Event()

    def fail_upsert(**kwargs):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("private-write-detail")

    writer._collection = SimpleNamespace(upsert=fail_upsert)
    task = asyncio.create_task(writer.save_text_memory("new note", None))
    try:
        for _ in range(1000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store)
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="private-write-detail"):
            await task
        writer._executor.shutdown(wait=True)
    assert collection.count() == 513
    assert admin.list_memories(store, limit=0) == {"memories": [], "total": 513}


def test_case_variant_paths_share_collection_state_only_when_same_directory(tmp_path):
    from sqllens._memory_consistency import collection_state

    actual = tmp_path / "MixedCase"
    actual.mkdir()
    variant = tmp_path / "mixedcase"
    if variant.exists():
        # Case-insensitive filesystem: both spellings name one directory.
        assert collection_state(str(actual), "one") is collection_state(str(variant), "one")
    else:
        # Case-sensitive filesystem: the other spelling is a separate directory.
        variant.mkdir()
        assert collection_state(str(actual), "one") is not collection_state(str(variant), "one")


async def test_one_completed_writer_does_not_hide_another_active_writer(populated_store):
    store, _, _, _ = populated_store
    writer = ChromaAgentMemory(store._mem.persist_directory, store._mem.collection_name)
    entered = {name: threading.Event() for name in ("first", "second")}
    release = {name: threading.Event() for name in entered}

    def upsert(*, documents, **kwargs):
        name = documents[0]
        entered[name].set()
        assert release[name].wait(10)

    writer._collection = SimpleNamespace(upsert=upsert)
    tasks = {name: asyncio.create_task(writer.save_text_memory(name, None)) for name in entered}
    try:
        for _ in range(1000):
            if all(event.is_set() for event in entered.values()):
                break
            await asyncio.sleep(0.001)
        assert all(event.is_set() for event in entered.values())
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store)
        release["first"].set()
        await tasks["first"]
        with pytest.raises(RuntimeError, match=r"changed.*retry"):
            admin.list_memories(store)
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks.values())
        writer._executor.shutdown(wait=True)
    assert admin.list_memories(store, limit=0) == {"memories": [], "total": 513}
