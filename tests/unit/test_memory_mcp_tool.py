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
