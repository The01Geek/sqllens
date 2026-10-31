# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory import requires explicit registration and write authorization."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from sqllens.config import AuthConfig, Config
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings


def _cfg(tmp_path, *, allow_import: bool, auth: AuthConfig | None = None) -> Config:
    return build_test_config(
        tmp_path / "chroma",
        allow_import=allow_import,
        auth=auth or AuthConfig(
            mode="bearer", bearer_token=SecretStr("test-token-01234567890123456789012")
        ),
    )


async def _tool_names(mcp) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


async def test_tool_absent_by_default(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_import=False)))
    assert "import_memory" not in names
    assert {"query_database", "list_data_sources"} <= names


async def test_tool_present_when_enabled(tmp_path) -> None:
    names = await _tool_names(build_server(_cfg(tmp_path, allow_import=True)))
    assert "import_memory" in names


@pytest.mark.parametrize("auth", [
    AuthConfig(mode="bearer", bearer_token=SecretStr("test-token-01234567890123456789012")),
    AuthConfig(mode="none", insecure=True),
])
async def test_tool_imports_and_reports(tmp_path, monkeypatch, auth) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_import=True, auth=auth))
    result = await mcp.call_tool(
        "import_memory",
        {"bundle_json": '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}'},
    )
    text = str(result)
    assert "| saved | 1 |" in text
    assert "| skipped (duplicate) | 0 |" in text
    assert "| errors | 0 |" in text


@pytest.mark.parametrize("bundle_json", ["{ not json", '{"schema_docs": [{"content": "test"}]}'])
async def test_tool_refuses_unauthenticated_import_before_parsing(
    tmp_path, monkeypatch, bundle_json
) -> None:
    from unittest.mock import AsyncMock, Mock

    parse = Mock(side_effect=AssertionError("unauthorized bundle was parsed"))
    import_bundle = AsyncMock(side_effect=AssertionError("unauthorized import reached storage"))
    monkeypatch.setattr("sqllens.memory.io.parse_json", parse)
    monkeypatch.setattr("sqllens.memory.import_bundle", import_bundle)
    mcp = build_server(_cfg(tmp_path, allow_import=True, auth=AuthConfig(mode="none")))

    with pytest.raises(Exception, match="disabled on an unauthenticated endpoint"):
        await mcp.call_tool("import_memory", {"bundle_json": bundle_json})

    parse.assert_not_called()
    import_bundle.assert_not_awaited()


async def test_tool_errors_on_bad_input(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_import=True))
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool("import_memory", {"bundle_json": "{ not json"})
    assert "Invalid memory bundle" in str(excinfo.value)


async def test_tool_errors_on_oversize_bundle(tmp_path, monkeypatch) -> None:
    """The DoS-cap BundleFormatError surfaces as isError:true at the MCP
    boundary — never as an unguarded crash or a silent success."""
    from sqllens.memory.schema import MAX_BUNDLE_BYTES

    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_import=True))
    # Pad just past the byte cap with cheap ASCII so the size-cap branch fires
    # (not the JSON-parse branch).
    oversize = "x" * (MAX_BUNDLE_BYTES + 1)
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool("import_memory", {"bundle_json": oversize})
    msg = str(excinfo.value)
    assert "Invalid memory bundle" in msg
    assert "exceeds" in msg


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
    msg = str(excinfo.value)
    assert "import failed" in msg.lower()
    assert "(0 saved" in msg


async def test_tool_signals_error_on_partial_failure(
    tmp_path, monkeypatch
) -> None:
    """A run that saved some items but errored on others is a failed import:
    per the AGENTS.md 'partial failure is failure' rule it must reach the
    client as isError:true, not a success string that merely reports the count.
    """
    patch_fake_embeddings(monkeypatch)
    cfg = _cfg(tmp_path, allow_import=True)

    async def fail_schema_doc(self, content: str) -> None:
        raise RuntimeError("boom")

    # sql_pair saves fine; only the schema_doc save errors. A generic
    # RuntimeError is recorded per-item (not systemic), so the import yields
    # saved=1, errors=1 — exactly the partial-failure shape that previously
    # slipped through the saved==0 guard as an isError:false success.
    monkeypatch.setattr(
        "sqllens.memory.store.MemoryStore.add_schema_doc", fail_schema_doc
    )
    mcp = build_server(cfg)
    with pytest.raises(Exception) as excinfo:
        await mcp.call_tool(
            "import_memory",
            {
                "bundle_json": (
                    '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}, '
                    '"schema_docs": [{"content": "a schema doc"}]}'
                )
            },
        )
    msg = str(excinfo.value)
    assert "import failed" in msg.lower()
    assert "(1 saved" in msg


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
