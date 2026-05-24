# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The memory-administration MCP tools, gated by SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS.

These tools (list/get/delete/clear/add/export/stats) curate the training set.
The destructive subset additionally refuses to run on an unauthenticated
endpoint. The tool closures are invoked directly via the tool manager's ``.fn``
(the established pattern in test_server.py): FastMCP.call_tool collapses the
return into content blocks, but here we assert the structured JSON / isError
branch the closure produces.
"""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult

from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.config import AuthConfig, Config
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings

pytestmark = pytest.mark.asyncio

_ADMIN_TOOLS = {
    "list_memories",
    "get_memory",
    "delete_memory",
    "clear_memories",
    "add_memories",
    "export_memories",
    "get_memory_stats",
}

_DSID = "ds-test-uuid"


def _cfg(tmp_path, *, allow_admin_tools: bool, auth: AuthConfig | None = None) -> Config:
    # insecure=True acknowledges a closed network so the destructive tools are
    # callable under the default mode="none" used by most of these tests.
    return build_test_config(
        tmp_path / "chroma",
        allow_admin_tools=allow_admin_tools,
        auth=auth or AuthConfig(mode="none", insecure=True),
    )


def _fn(mcp, name: str):
    """The raw async closure FastMCP registered for ``name``."""
    return mcp._tool_manager.get_tool(name).fn


async def _tool_names(mcp) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


def _parse(result) -> dict:
    """Decode a tool return into its JSON payload (str or CallToolResult body)."""
    if isinstance(result, CallToolResult):
        return json.loads(result.content[0].text)
    return json.loads(result)


# --- Registration / gating ----------------------------------------------------


async def test_admin_tools_absent_by_default(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_admin_tools=False)))
    assert _ADMIN_TOOLS.isdisjoint(names)
    assert {"query_database", "list_data_sources"} <= names


async def test_admin_tools_present_when_enabled(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_admin_tools=True)))
    assert _ADMIN_TOOLS <= names


# --- add_memories + list_memories ---------------------------------------------


async def test_add_then_list(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))

    add = await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[
            {"question": "how many users?", "sql": "SELECT count(*) FROM users"},
            {"question": "list orders", "sql": "SELECT * FROM orders"},
        ],
        schema_docs=[{"content": "users(id, name)"}],
    )
    added = _parse(add)
    assert added["saved_count"] == 3
    assert added["duplicate_count"] == 0
    assert added["errors"] == []

    listed = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert listed["total"] == 3
    questions = {m.get("question") for m in listed["memories"]}
    assert "how many users?" in questions
    sql_pairs = [m for m in listed["memories"] if m["memory_type"] == "tool_usage"]
    docs = [m for m in listed["memories"] if m["memory_type"] == "text"]
    assert len(sql_pairs) == 2
    assert len(docs) == 1
    assert any(m["sql"] == "SELECT count(*) FROM users" for m in sql_pairs)


async def test_list_filters_by_type(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[{"question": "q", "sql": "SELECT 1"}],
        schema_docs=[{"content": "a doc"}],
    )
    tool_only = _parse(
        await _fn(mcp, "list_memories")(data_source_id=_DSID, memory_type="tool_usage")
    )
    assert tool_only["total"] == 1
    assert tool_only["memories"][0]["memory_type"] == "tool_usage"

    text_only = _parse(
        await _fn(mcp, "list_memories")(data_source_id=_DSID, memory_type="text")
    )
    assert text_only["total"] == 1
    assert text_only["memories"][0]["content"] == "a doc"


async def test_list_rejects_bad_memory_type(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    with pytest.raises(RuntimeError) as excinfo:
        await _fn(mcp, "list_memories")(data_source_id=_DSID, memory_type="bogus")
    assert "Unknown memory_type" in str(excinfo.value)


async def test_add_partial_failure_is_error(tmp_path, monkeypatch) -> None:
    """An invalid row makes add_memories an isError result, but the structured
    errors[] (with the original input index) is still returned."""
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    result = await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[
            {"question": "ok", "sql": "SELECT 1"},
            {"question": "bad", "sql": "   "},  # blank sql → validation error
        ],
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    payload = _parse(result)
    assert payload["saved_count"] == 1
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["index"] == 1
    assert payload["errors"][0]["question"] == "bad"


# --- get_memory / delete_memory -----------------------------------------------


async def test_get_memory_and_not_found(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID, sql_pairs=[{"question": "q1", "sql": "SELECT 1"}]
    )
    listed = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    memory_id = listed["memories"][0]["memory_id"]

    got = _parse(await _fn(mcp, "get_memory")(data_source_id=_DSID, memory_id=memory_id))
    assert got["memory_id"] == memory_id
    assert got["question"] == "q1"
    # full embedding included
    assert "embedding" in got and got["embedding_dim"] == len(got["embedding"])

    missing = await _fn(mcp, "get_memory")(data_source_id=_DSID, memory_id="nope")
    assert isinstance(missing, CallToolResult)
    assert missing.isError is True
    assert _parse(missing)["error"] == "memory not found"


async def test_delete_memory(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID, sql_pairs=[{"question": "q", "sql": "SELECT 1"}]
    )
    listed = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    memory_id = listed["memories"][0]["memory_id"]

    deleted = _parse(
        await _fn(mcp, "delete_memory")(data_source_id=_DSID, memory_id=memory_id)
    )
    assert deleted == {"deleted": True}

    again = await _fn(mcp, "delete_memory")(data_source_id=_DSID, memory_id=memory_id)
    assert isinstance(again, CallToolResult)
    assert again.isError is True
    assert _parse(again)["deleted"] is False


async def test_clear_by_type(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[{"question": "q", "sql": "SELECT 1"}],
        schema_docs=[{"content": "doc"}],
    )
    cleared = _parse(
        await _fn(mcp, "clear_memories")(data_source_id=_DSID, memory_type="text")
    )
    assert cleared == {"deleted_count": 1}
    remaining = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert remaining["total"] == 1
    assert remaining["memories"][0]["memory_type"] == "tool_usage"


# --- export round-trip --------------------------------------------------------


async def test_export_json_roundtrips_into_add(tmp_path, monkeypatch) -> None:
    """export_memories(JSON) output feeds straight back into add_memories."""
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[
            {"question": "q1", "sql": "SELECT 1"},
            {"question": "q2", "sql": "SELECT 2"},
        ],
        schema_docs=[{"content": "schema doc one"}],
    )

    exported = _parse(
        await _fn(mcp, "export_memories")(data_source_id=_DSID, format="json")
    )
    blob = json.loads(exported["data"])
    assert {"sql_pairs", "schema_docs"} == set(blob.keys())

    # Feed the export back into a *fresh* store unmodified → everything saves.
    mcp2 = build_server(_cfg(tmp_path / "second", allow_admin_tools=True))
    re_added = _parse(
        await _fn(mcp2, "add_memories")(data_source_id=_DSID, **blob)
    )
    assert re_added["saved_count"] == 3
    assert re_added["errors"] == []


async def test_export_empty_store_warns_but_succeeds(tmp_path, monkeypatch) -> None:
    """An empty store is not data loss (nothing existed) — it exports as a
    non-fatal success carrying an explanatory warning, matching the CLI."""
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    result = await _fn(mcp, "export_memories")(data_source_id=_DSID, format="json")
    assert isinstance(result, str)
    payload = _parse(result)
    assert payload["lossy"] is False
    assert any("empty" in w for w in payload["warnings"])


async def test_export_csv_dropping_schema_docs_is_lossy(tmp_path, monkeypatch) -> None:
    """CSV can't carry schema docs; dropping ones that EXIST is genuine partial
    loss → isError, with the warning surfaced in the body."""
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[{"question": "q", "sql": "SELECT 1"}],
        schema_docs=[{"content": "doc that CSV cannot carry"}],
    )
    result = await _fn(mcp, "export_memories")(data_source_id=_DSID, format="csv")
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    payload = _parse(result)
    assert payload["lossy"] is True
    assert any("schema doc" in w for w in payload["warnings"])


# --- hit tracking -------------------------------------------------------------


async def test_hit_count_advances_on_retrieval(tmp_path, monkeypatch) -> None:
    """hit_count / last_hit_date advance when a memory is retrieved via the same
    search path query_database uses (search_similar_usage)."""
    patch_fake_embeddings(monkeypatch)
    cfg = _cfg(tmp_path, allow_admin_tools=True)
    mcp = build_server(cfg)
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[{"question": "how many users?", "sql": "SELECT count(*) FROM users"}],
    )

    # Reach the same retrieval method the agent's search tool calls at query
    # time. A second MemoryStore over the same persist dir/collection sees the
    # rows the admin tools wrote and shares their on-disk hit_count.
    from sqllens.memory import MemoryStore

    probe = MemoryStore(cfg)
    ctx = ToolContext(
        user=User(id="t"), conversation_id="c", request_id="r", agent_memory=probe._mem
    )

    before = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert before["memories"][0]["hit_count"] == 0
    assert before["memories"][0]["last_hit_date"] is None

    hits = await probe._mem.search_similar_usage(
        question="how many users?", context=ctx, limit=10, similarity_threshold=0.0
    )
    assert len(hits) == 1

    after = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert after["memories"][0]["hit_count"] == 1
    assert after["memories"][0]["last_hit_date"] is not None

    await probe._mem.search_similar_usage(
        question="how many users?", context=ctx, limit=10, similarity_threshold=0.0
    )
    after2 = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert after2["memories"][0]["hit_count"] == 2


async def test_get_memory_stats(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _cfg(tmp_path, allow_admin_tools=True)
    mcp = build_server(cfg)
    await _fn(mcp, "add_memories")(
        data_source_id=_DSID,
        sql_pairs=[{"question": "q1", "sql": "SELECT 1"}],
        schema_docs=[{"content": "doc"}],
    )

    from sqllens.memory import MemoryStore

    probe = MemoryStore(cfg)
    ctx = ToolContext(
        user=User(id="t"), conversation_id="c", request_id="r", agent_memory=probe._mem
    )
    await probe._mem.search_similar_usage(
        question="q1", context=ctx, limit=10, similarity_threshold=0.0
    )

    stats = _parse(await _fn(mcp, "get_memory_stats")(data_source_id=_DSID))
    assert stats["tool_usage_count"] == 1
    assert stats["text_count"] == 1
    assert stats["total_hits_last_30d"] == 1
    assert len(stats["top_hit_memories"]) == 1
    assert stats["top_hit_memories"][0]["hit_count"] == 1


# --- auth gating on destructive tools -----------------------------------------


async def test_destructive_tools_blocked_without_auth(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    # mode="none" and NOT insecure → destructive tools must refuse.
    cfg = build_test_config(
        tmp_path / "chroma",
        allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=False),
    )
    mcp = build_server(cfg)

    for name, kwargs in (
        ("add_memories", {"sql_pairs": [{"question": "q", "sql": "SELECT 1"}]}),
        ("delete_memory", {"memory_id": "x"}),
        ("clear_memories", {}),
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await _fn(mcp, name)(data_source_id=_DSID, **kwargs)
        assert "unauthenticated endpoint" in str(excinfo.value)

    # Read-only tools stay available even without auth.
    listed = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    assert listed["total"] == 0


async def test_destructive_tools_allowed_with_bearer_auth(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = build_test_config(
        tmp_path / "chroma",
        allow_admin_tools=True,
        auth=AuthConfig(mode="bearer", bearer_token="s3cr3t-token-value"),
    )
    mcp = build_server(cfg)
    added = _parse(
        await _fn(mcp, "add_memories")(
            data_source_id=_DSID, sql_pairs=[{"question": "q", "sql": "SELECT 1"}]
        )
    )
    assert added["saved_count"] == 1
