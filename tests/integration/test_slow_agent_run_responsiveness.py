# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Quick requests stay responsive while a slow agent run is in flight.

An MCP client may call ``list_data_sources`` before each question, and a
container health check polls ``/healthz`` every few seconds. Both are cheap,
but they share the server's single event loop with ``query_database``. If the
agent's model call blocks that loop, every other request waits until the
model answers, and short client timeouts fail.

This test points the real Anthropic SDK at a local stand-in that holds each
``/v1/messages`` request open until the test releases it. No real model is
called and no real credential is needed. While the agent run is held,
``/healthz`` and ``list_data_sources`` on a second MCP session must both
answer within a short timeout.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from sqllens.config import AuthConfig
from sqllens.tools import _agent as agent_module
from tests.unit._memory_helpers import patch_fake_embeddings

QUICK_TIMEOUT_S = 2.0
"""Budget for each quick request. Matches the container health check timeout."""

HOLD_LIMIT_S = 30.0
"""Upper bound on how long the stand-in holds a request if never released."""

_MESSAGE = {
    "id": "msg-slow",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _sse_body() -> bytes:
    start = {**_MESSAGE, "content": [], "stop_reason": None}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Done."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


class _SlowModel:
    """Local stand-in for the Messages API that holds requests until released."""

    def __init__(self) -> None:
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.hold_expired = False
        """True if a request was answered because the hold limit ran out."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.arrived.set()
                if not outer.release.wait(HOLD_LIMIT_S):
                    outer.hold_expired = True
                if payload.get("stream"):
                    body, ctype = _sse_body(), "text/event-stream"
                else:
                    body, ctype = json.dumps(_MESSAGE).encode(), "application/json"
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def slow_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SlowModel]:
    model = _SlowModel()
    # The agent reads the base URL when the server builds it at startup.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", model.url)
    # Build a fresh agent for this server; another test may have cached one.
    monkeypatch.setattr(agent_module, "_AGENT_STATE", None)
    patch_fake_embeddings(monkeypatch)
    try:
        yield model
    finally:
        model.close()


async def test_health_and_list_data_sources_answer_during_slow_agent_run(
    make_server, slow_model: _SlowModel
) -> None:
    handle = make_server(AuthConfig(mode="none"))

    async with streamablehttp_client(handle.mcp_url) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        run = asyncio.create_task(
            session.call_tool("query_database", {"question": "How many artists are there?"})
        )
        try:
            arrived = await asyncio.to_thread(slow_model.arrived.wait, 15)
            assert arrived, "the agent run never reached the model"
            assert not run.done()

            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{handle.base_url}/healthz", timeout=QUICK_TIMEOUT_S)
            assert resp.status_code == 200

            async def list_sources() -> str:
                async with streamablehttp_client(handle.mcp_url) as (r, w, _), \
                        ClientSession(r, w) as quick:
                    await quick.initialize()
                    result = await quick.call_tool("list_data_sources", {})
                    assert not result.isError
                    content = result.content[0]
                    assert isinstance(content, TextContent)
                    return content.text

            quick = asyncio.create_task(list_sources())
            done, _ = await asyncio.wait({quick}, timeout=QUICK_TIMEOUT_S)
            if not done:
                quick.cancel()
                await asyncio.gather(quick, return_exceptions=True)
                pytest.fail(f"list_data_sources did not answer within {QUICK_TIMEOUT_S} s")
            assert "chinook-test" in quick.result()
            # The quick requests finished while the agent run was still held.
            assert not run.done()
        except BaseException:
            # Stop the held run before the session closes, so the failure
            # above is what the test reports rather than teardown errors.
            slow_model.release.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            raise

        slow_model.release.set()
        await asyncio.wait_for(run, HOLD_LIMIT_S)

    assert not slow_model.hold_expired, "the stand-in answered only after its hold limit"
