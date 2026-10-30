# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Event-loop liveness tests for the ``query_database`` blocking leaf calls.

Each test blocks a *fake synchronous* driver or Anthropic client inside the
awaited call and asserts that an unrelated coroutine on the same event loop
still makes progress while it is blocked. Before issue #255 the loop was stuck
for the whole duration of every such call; after it, the blocking body runs on
a worker thread and the loop keeps serving.

Every test releases its ``threading.Event`` in a ``finally``: a thread left
blocked would keep the interpreter from exiting, because shutdown joins the
default executor's threads.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.runtime import (
    EffectiveSettings,
    reset_effective_settings,
    set_effective_settings,
)

# How long a liveness probe waits for the blocked call to release the loop.
# Generous enough for a loaded CI runner; a pre-fix run fails by timing out
# here rather than by a flaky-fast threshold.
_LIVENESS_TIMEOUT_SECONDS = 5.0


async def _assert_loop_live_while(blocked: asyncio.Task, release: threading.Event) -> None:
    """Assert the loop still runs a probe coroutine while ``blocked`` is blocked.

    The probe is a plain ``asyncio.sleep(0)`` round trip: if the blocking call
    held the loop thread, the probe could not complete until the call returned,
    so ``wait_for`` would time out. Releases ``release`` and awaits ``blocked``
    either way, so no worker thread is left blocked.
    """
    try:
        probe_ran = False

        async def _probe() -> None:
            nonlocal probe_ran
            await asyncio.sleep(0)
            probe_ran = True

        await asyncio.wait_for(_probe(), timeout=_LIVENESS_TIMEOUT_SECONDS)
        assert probe_ran, "probe coroutine did not run while the call was blocked"
        assert not blocked.done(), "the call returned before the probe; nothing was blocked"
    finally:
        release.set()
        await blocked


def _blocking_cursor(release: threading.Event, rows: list[dict[str, Any]]) -> MagicMock:
    """A cursor whose ``execute`` blocks until ``release`` is set."""
    cursor = MagicMock()

    def _execute(_sql: Any) -> None:
        assert release.wait(timeout=30), "release event was never set"

    cursor.execute.side_effect = _execute
    cursor.fetchmany.return_value = rows
    return cursor


# ---------------------------------------------------------------------------
# Database runners
# ---------------------------------------------------------------------------


def _install_fake_pymysql(monkeypatch: pytest.MonkeyPatch, conn: MagicMock) -> None:
    pymysql = types.ModuleType("pymysql")
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = type("DictCursor", (), {})
    cursors_mod.SSDictCursor = type("SSDictCursor", (), {})
    pymysql.cursors = cursors_mod
    pymysql.connect = lambda **_kwargs: conn
    pymysql.Error = Exception
    monkeypatch.setitem(sys.modules, "pymysql", pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_mod)


def _install_fake_psycopg2(monkeypatch: pytest.MonkeyPatch, conn: MagicMock) -> None:
    psycopg2 = types.ModuleType("psycopg2")
    extras_mod = types.ModuleType("psycopg2.extras")
    extras_mod.RealDictCursor = type("RealDictCursor", (), {})
    psycopg2.extras = extras_mod
    extensions_mod = types.ModuleType("psycopg2.extensions")
    extensions_mod.parse_dsn = lambda _dsn: {}
    psycopg2.extensions = extensions_mod
    psycopg2.connect = lambda *_a, **_k: conn
    psycopg2.Error = Exception
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extensions", extensions_mod)


async def test_mysql_runner_keeps_event_loop_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    conn = MagicMock()
    conn.cursor.return_value = _blocking_cursor(release, [{"x": 1}])
    _install_fake_pymysql(monkeypatch, conn)

    from sqllens.agent.integrations.mysql.sql_runner import MySQLRunner

    runner = MySQLRunner(host="h", database="d", user="u", password="p")
    blocked = asyncio.create_task(
        runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())
    )
    await asyncio.sleep(0.05)  # let the task reach the blocking call

    await _assert_loop_live_while(blocked, release)

    assert blocked.result().to_dict(orient="records") == [{"x": 1}]


async def test_postgres_runner_keeps_event_loop_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    conn = MagicMock()
    conn.cursor.return_value = _blocking_cursor(release, [{"x": 1}])
    _install_fake_psycopg2(monkeypatch, conn)

    from sqllens.agent.integrations.postgres.sql_runner import PostgresRunner

    runner = PostgresRunner(connection_string="postgresql://u:p@h/d")
    blocked = asyncio.create_task(
        runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())
    )
    await asyncio.sleep(0.05)

    await _assert_loop_live_while(blocked, release)

    assert blocked.result().to_dict(orient="records") == [{"x": 1}]


async def test_sqlite_runner_keeps_event_loop_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    conn = MagicMock()
    conn.cursor.return_value = _blocking_cursor(release, [{"x": 1}])
    # ``PRAGMA query_only`` read-back happens on the connection, not the cursor.
    conn.execute.return_value.fetchone.return_value = [1]

    from sqllens.agent.integrations.sqlite import sql_runner as sqlite_module

    monkeypatch.setattr(
        sqlite_module.sqlite3, "connect", lambda *_a, **_k: conn
    )

    runner = sqlite_module.SqliteRunner(database_path="/tmp/does-not-matter.db")
    blocked = asyncio.create_task(
        runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())
    )
    await asyncio.sleep(0.05)

    await _assert_loop_live_while(blocked, release)

    assert blocked.result().to_dict(orient="records") == [{"x": 1}]


# ---------------------------------------------------------------------------
# Anthropic LLM service
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Minimal stand-in for an Anthropic ``Message`` response."""

    def __init__(self, text: str) -> None:
        self.content = [{"type": "text", "text": text}]
        self.stop_reason = "end_turn"
        self.usage = None


class _FakeStream:
    """Synchronous stream context manager mirroring the SDK's shape."""

    def __init__(self, chunks: list[str], release: threading.Event | None) -> None:
        self._chunks = chunks
        self._release = release

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    @property
    def text_stream(self):
        if self._release is not None:
            assert self._release.wait(timeout=30), "release event was never set"
        return iter(self._chunks)

    def get_final_message(self) -> _FakeMessage:
        return _FakeMessage("".join(self._chunks))


def _llm_service_with_fake_client(client: Any):
    """Build an ``AnthropicLlmService`` whose synchronous client is ``client``."""
    from sqllens.agent.integrations.anthropic.llm import AnthropicLlmService

    service = AnthropicLlmService(api_key="sk-ant-not-used")
    service._client = client
    return service


def _llm_request(prompt: str = "hi"):
    from sqllens.agent.core.llm import LlmRequest
    from sqllens.agent.core.llm.models import LlmMessage
    from sqllens.agent.core.user import User

    return LlmRequest(
        messages=[LlmMessage(role="user", content=prompt)],
        user=User(id="test-user", group_memberships=[]),
    )


async def test_anthropic_send_request_keeps_event_loop_live() -> None:
    release = threading.Event()
    client = MagicMock()

    def _create(**_payload: Any) -> _FakeMessage:
        assert release.wait(timeout=30), "release event was never set"
        return _FakeMessage("answer")

    client.messages.create.side_effect = _create

    service = _llm_service_with_fake_client(client)
    blocked = asyncio.create_task(service.send_request(_llm_request()))
    await asyncio.sleep(0.05)

    await _assert_loop_live_while(blocked, release)

    assert blocked.result().content == "answer"


async def test_anthropic_stream_request_keeps_event_loop_live() -> None:
    release = threading.Event()
    client = MagicMock()
    client.messages.stream.side_effect = lambda **_payload: _FakeStream(
        ["one", "two"], release
    )

    service = _llm_service_with_fake_client(client)

    async def _drain() -> list[Any]:
        return [chunk async for chunk in service.stream_request(_llm_request())]

    blocked = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)

    await _assert_loop_live_while(blocked, release)

    chunks = blocked.result()
    assert [c.content for c in chunks if c.content] == ["one", "two"]
    assert chunks[-1].finish_reason == "end_turn"


async def test_anthropic_stream_request_chunks_match_prior_shape() -> None:
    """The yielded chunk sequence is unchanged by the offload (criterion 7).

    One chunk per non-empty text piece, then a final chunk carrying the finish
    reason and provider content. Ordering and payloads are what the pre-#255
    generator produced for the same fake stream.
    """
    client = MagicMock()
    client.messages.stream.side_effect = lambda **_payload: _FakeStream(
        ["a", "", "b"], None
    )

    service = _llm_service_with_fake_client(client)
    chunks = [chunk async for chunk in service.stream_request(_llm_request())]

    assert [c.content for c in chunks] == ["a", "b", None]
    assert chunks[-1].finish_reason == "end_turn"
    assert chunks[-1].provider_content == [{"type": "text", "text": "ab"}]
    assert all(c.tool_calls is None for c in chunks)


# ---------------------------------------------------------------------------
# Per-request profile settings reach the worker thread
# ---------------------------------------------------------------------------


async def test_profile_row_cap_reaches_offloaded_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.to_thread`` carries the request-local ``ContextVar`` (criterion 9).

    ``loop.run_in_executor`` would not, and the runner would silently fetch the
    base ``max_rows`` instead of the narrower per-profile cap.
    """
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = None
    cursor.fetchmany.return_value = [{"x": 1}]
    conn.cursor.return_value = cursor
    _install_fake_psycopg2(monkeypatch, conn)

    from sqllens.agent.integrations.postgres.sql_runner import PostgresRunner

    runner = PostgresRunner(connection_string="postgresql://u:p@h/d", max_rows=10_000)

    token = set_effective_settings(
        EffectiveSettings(
            show_details=False,
            max_tool_iterations=10,
            max_rows=7,
            similarity_threshold=0.5,
        )
    )
    try:
        await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())
    finally:
        reset_effective_settings(token)

    cursor.fetchmany.assert_called_once_with(8)
