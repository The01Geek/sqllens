# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Bound memory work before writes and avoid materializing an entire listing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from sqllens._memory_consistency import CollectionState
from sqllens.agent.core.registry import ToolRegistry
from sqllens.agent.core.tool import ToolCall, ToolContext
from sqllens.agent.core.user import User
from sqllens.agent.tools.agent_memory import (
    SearchSavedCorrectToolUsesParams,
    SearchSavedCorrectToolUsesTool,
)
from sqllens.memory import admin
from sqllens.memory.schema import MAX_BUNDLE_ITEMS, MemoryBundle
from sqllens.memory.store import MemoryStore

from ._agent_stubs import StubAgentMemory


@pytest.mark.parametrize("kind", ["sql_pairs", "schema_docs"])
async def test_oversized_add_rejected_before_validation_or_store_work(kind):
    store = Mock()
    with pytest.raises(ValueError, match="item cap"):
        await admin.add_memories(store, **{kind: [{}] * (MAX_BUNDLE_ITEMS + 1)})
    assert store.mock_calls == []


@pytest.mark.parametrize(
    "kind,item,method",
    [
        ("sql_pairs", {"question": "valid", "sql": "SELECT 1"}, "add_sql_pair"),
        ("schema_docs", {"content": "valid schema note"}, "add_schema_doc"),
    ],
)
async def test_exact_add_boundary_preserves_deduplication(kind, item, method):
    store = Mock()
    store.iter_all.return_value = MemoryBundle()
    writer = AsyncMock()
    setattr(store, method, writer)
    result = await admin.add_memories(store, **{kind: [item] * MAX_BUNDLE_ITEMS})
    assert result["saved_count"] == 1
    assert result["duplicate_count"] == MAX_BUNDLE_ITEMS - 1
    assert result["errors"] == []
    writer.assert_awaited_once()


async def test_mixed_errors_keep_indices_and_counts_without_raw_details():
    store = Mock()
    store.iter_all.return_value = MemoryBundle()
    store.add_sql_pair = AsyncMock(side_effect=[None, RuntimeError("synthetic-private-path")])
    result = await admin.add_memories(
        store,
        sql_pairs=[
            {"question": "valid", "sql": "SELECT 1"},
            {"question": "invalid", "sql": {"secret": "synthetic-secret-canary"}},
            {"question": "failed write", "sql": "SELECT 2"},
        ],
    )
    assert result["saved_count"] == 1
    assert [error["index"] for error in result["errors"]] == [1, 2]
    assert "synthetic-secret-canary" not in json.dumps(result)
    assert "synthetic-private-path" not in json.dumps(result)
    assert "sql:" in result["errors"][0]["error"]
    assert "Failed to save" in result["errors"][1]["error"]


class PagedCollection:
    def __init__(self, count=1200):
        self.count = count
        self.calls = []

    def get(self, *, include, limit=None, offset=0):
        self.calls.append((limit, offset, include))
        assert limit is not None and 0 < limit <= 256, "unbounded collection read"
        rows = list(range(offset, min(offset + limit, self.count)))
        return {
            "ids": [str(i) for i in rows],
            "metadatas": [
                {"timestamp": f"{i:06d}", "is_text_memory": i % 2 == 1, "content": f"note {i}"}
                for i in rows
            ],
            "embeddings": [[float(i)] * 12 for i in rows] if "embeddings" in include else None,
        }


def store_with_pages(collection):
    store = object.__new__(MemoryStore)
    store._mem = SimpleNamespace(
        _get_collection=lambda: collection, _collection_state=CollectionState()
    )
    return store


def test_listing_pages_store_and_preserves_newest_first_total_and_preview():
    collection = PagedCollection()
    result = admin.list_memories(
        store_with_pages(collection),
        memory_type="text",
        limit=3,
        include_embedding_preview=True,
    )
    assert result["total"] == 600
    assert [row["memory_id"] for row in result["memories"]] == ["1199", "1197", "1195"]
    assert result["memories"][0]["embedding_preview"] == [1199.0] * 8
    assert result["memories"][0]["embedding_dim"] == 12
    assert len(collection.calls) == 5


def test_zero_limit_still_counts_matching_rows():
    collection = PagedCollection()
    result = admin.list_memories(store_with_pages(collection), limit=0)
    assert result == {"memories": [], "total": 1200}
    assert all("embeddings" not in call[2] for call in collection.calls)


def test_default_listing_limit_and_equal_timestamp_order():
    collection = PagedCollection()
    original_get = collection.get

    def same_timestamp(**kwargs):
        rows = original_get(**kwargs)
        for metadata in rows["metadatas"]:
            metadata["timestamp"] = "same"
        return rows

    collection.get = same_timestamp
    result = admin.list_memories(store_with_pages(collection))
    assert result["total"] == 1200
    assert [row["memory_id"] for row in result["memories"]] == [str(i) for i in range(1000)]


@pytest.mark.parametrize("limit", [-1, 1001, None])
def test_invalid_listing_limit_rejected_before_storage(limit):
    collection = PagedCollection()
    with pytest.raises(ValueError, match="limit"):
        admin.list_memories(store_with_pages(collection), limit=limit)
    assert collection.calls == []


@pytest.mark.parametrize("limit", [0, -1, 101, 1000000])
def test_search_limit_rejects_out_of_range_values(limit):
    with pytest.raises(ValidationError):
        SearchSavedCorrectToolUsesParams(question="sample", limit=limit)


@pytest.mark.parametrize("limit", [None, 1, 10, 100])
def test_search_limit_preserves_supported_values(limit):
    assert SearchSavedCorrectToolUsesParams(question="sample", limit=limit).limit == limit


@pytest.mark.parametrize("limit", [None, 1, 100, 101])
async def test_registry_search_limit_signal_and_backend_calls(limit):
    memory = StubAgentMemory()
    memory.search_similar_usage = AsyncMock(return_value=[])
    context = ToolContext(
        user=User(id="test"), conversation_id="c", request_id="r", agent_memory=memory
    )
    registry = ToolRegistry()
    registry.register_local_tool(SearchSavedCorrectToolUsesTool(), access_groups=[])
    result = await registry.execute(
        ToolCall(
            id="test",
            name="search_saved_correct_tool_uses",
            arguments={"question": "test", "limit": limit},
        ),
        context,
    )
    if limit == 101:
        assert result.success is False
        assert "limit" in result.error and "100" in result.error
        memory.search_similar_usage.assert_not_awaited()
    else:
        assert result.success is True
        assert memory.search_similar_usage.await_args.kwargs["limit"] == (limit or 10)
