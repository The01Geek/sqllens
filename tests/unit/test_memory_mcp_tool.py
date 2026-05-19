# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The import_memory MCP tool is gated by SQLLENS_MEMORY__ALLOW_IMPORT."""

from __future__ import annotations

import pytest

from sqllens.config import Config
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings


def _cfg(tmp_path, *, allow_import: bool) -> Config:
    return build_test_config(tmp_path / "chroma", allow_import=allow_import)


async def _tool_names(mcp) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


async def test_tool_absent_by_default(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_import=False)))
    assert "import_memory" not in names
    assert {"query_database", "list_data_sources"} <= names


async def test_tool_present_when_enabled(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_import=True)))
    assert "import_memory" in names


async def test_tool_imports_and_reports(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_import=True))
    result = await mcp.call_tool(
        "import_memory",
        {"bundle_json": '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}'},
    )
    text = str(result)
    assert "| saved | 1 |" in text
    assert "| skipped (duplicate) | 0 |" in text
    assert "| errors | 0 |" in text


async def test_tool_errors_on_bad_input(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_import=True))
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool("import_memory", {"bundle_json": "{ not json"})
    assert "Invalid memory bundle" in str(excinfo.value)


async def test_tool_signals_error_when_every_item_fails(
    tmp_path, monkeypatch
) -> None:
    """A run that saves nothing but collects per-item errors must reach the
    client as a failure, not an isError:false success."""
    patch_fake_embeddings(monkeypatch)
    cfg = _cfg(tmp_path, allow_import=True)

    async def always_fail(self, question: str, sql: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "sqllens.memory.store.MemoryStore.add_sql_pair", always_fail
    )
    mcp = build_server(cfg)
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool(
            "import_memory",
            {"bundle_json": '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}'},
        )
    assert "saved nothing" in str(excinfo.value)


async def test_tool_store_failure_does_not_leak_persist_path(
    tmp_path, monkeypatch
) -> None:
    """A Chroma/disk failure must reach the client sanitized — never the raw
    exception (which can carry the on-disk persist path)."""
    patch_fake_embeddings(monkeypatch)
    cfg = _cfg(tmp_path, allow_import=True)
    secret_path = str(tmp_path / "chroma")

    async def boom(self, question: str, sql: str) -> None:
        raise RuntimeError(f"chroma exploded at {secret_path}/internal.db")

    monkeypatch.setattr(
        "sqllens.memory.store.MemoryStore.add_schema_doc", boom
    )
    # schema_doc save raises a generic RuntimeError (not systemic) so it is
    # caught per-item; with zero saves the tool raises the masked message.
    monkeypatch.setattr(
        "sqllens.memory.store.MemoryStore.add_sql_pair", boom
    )
    mcp = build_server(cfg)
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool(
            "import_memory",
            {"bundle_json": '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}'},
        )
    msg = str(excinfo.value)
    assert secret_path not in msg
