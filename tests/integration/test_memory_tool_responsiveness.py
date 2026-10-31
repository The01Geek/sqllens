# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``/healthz`` stays responsive while a memory tool's store call is held.

The memory tools' synchronous ChromaDB calls run on the memory worker thread,
not the server's event loop. This test holds ``import_memory``'s dedup read open
and requires ``/healthz`` to answer within the container health check timeout.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from sqllens.config import AuthConfig
from sqllens.memory.store import MemoryStore
from tests.integration.test_slow_agent_run_responsiveness import HOLD_LIMIT_S, QUICK_TIMEOUT_S
from tests.unit._memory_helpers import patch_fake_embeddings


async def test_health_answers_while_a_memory_store_call_is_held(
    make_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    iter_all = MemoryStore.iter_all

    def held_iter_all(self: MemoryStore):  # type: ignore[no-untyped-def]
        entered.set()
        release.wait(HOLD_LIMIT_S)
        return iter_all(self)

    monkeypatch.setattr(MemoryStore, "iter_all", held_iter_all)
    handle = make_server(AuthConfig(mode="none", insecure=True), allow_import=True)

    async with streamablehttp_client(handle.mcp_url) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        run = asyncio.create_task(
            session.call_tool(
                "import_memory", {"bundle_json": '{"schema_docs": [{"content": "users(id)"}]}'}
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 15), "the store read never started"
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{handle.base_url}/healthz", timeout=QUICK_TIMEOUT_S)
            assert resp.status_code == 200
            assert not run.done()
        finally:
            # Release in every case: the memory worker is shared by the whole
            # test process, so a held call would stall every later memory test.
            release.set()

        result = await asyncio.wait_for(run, HOLD_LIMIT_S)

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert not result.isError, text
    assert "| saved | 1 |" in text
