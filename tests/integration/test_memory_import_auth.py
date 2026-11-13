# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory imports honor write authorization over the MCP wire protocol."""

from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from sqllens.config import AuthConfig
from sqllens.memory.io import parse_json
from sqllens.memory.schema import ImportReport


@pytest.mark.parametrize("mode,insecure,allowed", [
    ("none", False, False),
    ("none", True, True),
    ("bearer", False, True),
])
async def test_import_write_auth(make_server, monkeypatch, mode, insecure, allowed) -> None:
    token = "test-token-01234567890123456789012"
    auth = AuthConfig(
        mode=mode,
        insecure=insecure,
        bearer_token=SecretStr(token) if mode == "bearer" else None,
    )
    parse = Mock(wraps=parse_json)
    importer = AsyncMock(return_value=ImportReport(saved=1))
    monkeypatch.setattr("sqllens.memory.io.parse_json", parse)
    monkeypatch.setattr("sqllens.memory.import_bundle", importer)
    handle = make_server(auth, allow_import=True)
    headers = {"Authorization": f"Bearer {token}"} if mode == "bearer" else None
    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        streamable_http_client(handle.mcp_url, http_client=http_client) as (read, write),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "import_memory", {"bundle_json": '{"schema_docs": [{"content": "test"}]}'}
            )

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert result.is_error is not allowed
    if allowed:
        assert "| saved | 1 |" in text
        parse.assert_called_once()
        importer.assert_awaited_once()
    else:
        assert text == (
            "Error executing tool import_memory: This tool mutates the memory store "
            "and is disabled on an unauthenticated endpoint. Set auth.mode='bearer' "
            "(or auth.insecure=true on a closed network) to enable it."
        )
        parse.assert_not_called()
        importer.assert_not_awaited()
