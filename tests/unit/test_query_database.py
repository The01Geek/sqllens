# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.tools.query_database``.

Covers the module-level agent singleton's lifecycle, error surfacing through
``RuntimeError``, and resource cleanup of the ``send_message`` stream. No real
LLM or ChromaDB I/O — all agent behavior is provided by stubs from conftest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from sqllens.agent.core.components import UiComponent
from sqllens.config import Config
from sqllens.tools import query_database
from tests.unit.conftest import (
    StubAgent,
    make_dataframe_component,
    make_status_card_error_component,
    make_text_component,
)


def _build_agent_patch(
    monkeypatch: pytest.MonkeyPatch, agent: StubAgent
) -> MagicMock:
    """Patch ``query_database.build_agent`` to return ``agent``; return the mock."""
    mock = MagicMock(return_value=agent)
    monkeypatch.setattr(query_database, "build_agent", mock)
    return mock


def _empty_send_message_impl() -> Any:
    async def _impl(_ctx: Any, _q: str) -> AsyncIterator[UiComponent]:
        yield make_text_component("ok")

    return _impl


@pytest.mark.asyncio
async def test_first_call_builds_agent(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """Cold-start: the first call must build the agent exactly once."""
    stub = agent_stub_factory(_empty_send_message_impl())
    build_mock = _build_agent_patch(monkeypatch, stub)

    await query_database.query_database_impl(test_config, "select 1")

    assert build_mock.call_count == 1
    assert build_mock.call_args.args == (test_config,)


@pytest.mark.asyncio
async def test_second_call_reuses_singleton(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """Warm path: subsequent calls must reuse the cached agent."""
    stub = agent_stub_factory(_empty_send_message_impl())
    build_mock = _build_agent_patch(monkeypatch, stub)

    await query_database.query_database_impl(test_config, "select 1")
    await query_database.query_database_impl(test_config, "select 2")

    assert build_mock.call_count == 1
    assert len(stub.send_message_calls) == 2


@pytest.mark.asyncio
async def test_singleton_ignores_changed_cfg(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    tmp_path: Any,
    agent_stub_factory: Any,
) -> None:
    """Documents current (buggy) behavior: a different cfg does NOT rebuild.

    Regression target for a future fix that should rebuild — or reject — when
    cfg identity changes. If this test starts failing, the singleton has been
    made cfg-aware and the assertion should be inverted.
    """
    from pydantic import SecretStr

    from sqllens.config import (
        AgentRuntimeConfig,
        AuthConfig,
        DatabaseConfig,
        LLMConfig,
        MemoryConfig,
    )

    stub = agent_stub_factory(_empty_send_message_impl())
    build_mock = _build_agent_patch(monkeypatch, stub)

    other_cfg = Config(
        database=DatabaseConfig(url="sqlite:///other.db"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-other")),
        memory=MemoryConfig(persist_dir=tmp_path / "other-chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )

    await query_database.query_database_impl(test_config, "select 1")
    await query_database.query_database_impl(other_cfg, "select 1")

    assert build_mock.call_count == 1
    assert build_mock.call_args.args == (test_config,)


@pytest.mark.asyncio
async def test_build_agent_raises_leaves_singleton_none(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """A failed first build must not poison the cache — retry must succeed."""
    stub = agent_stub_factory(_empty_send_message_impl())
    calls = {"n": 0}

    def flaky_build(cfg: Config) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first build failed")
        return stub

    monkeypatch.setattr(query_database, "build_agent", flaky_build)

    with pytest.raises(RuntimeError, match="first build failed"):
        await query_database.query_database_impl(test_config, "select 1")

    assert query_database._AGENT is None, (
        "failed build must leave singleton None so a retry can succeed"
    )

    result = await query_database.query_database_impl(test_config, "select 1")
    assert calls["n"] == 2
    assert result == "ok"


@pytest.mark.asyncio
async def test_send_message_raises_surfaces_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """An agent failure must be wrapped in RuntimeError preserving the cause."""

    async def _failing(_ctx: Any, _q: str) -> AsyncIterator[UiComponent]:
        yield make_text_component("partial")
        raise ValueError("schema lookup failed")

    stub = agent_stub_factory(_failing)
    _build_agent_patch(monkeypatch, stub)

    with pytest.raises(RuntimeError, match="query_database failed") as excinfo:
        await query_database.query_database_impl(test_config, "select 1")

    cause = excinfo.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) == "schema lookup failed"


@pytest.mark.asyncio
async def test_is_error_status_card_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """A STATUS_CARD with status='error' must surface as RuntimeError."""

    async def _impl(_ctx: Any, _q: str) -> AsyncIterator[UiComponent]:
        yield make_text_component("looking up schema")
        yield make_status_card_error_component(
            title="SQL execution failed",
            description="permission denied on table users",
        )

    stub = agent_stub_factory(_impl)
    _build_agent_patch(monkeypatch, stub)

    with pytest.raises(RuntimeError, match="permission denied on table users"):
        await query_database.query_database_impl(test_config, "show users")


@pytest.mark.asyncio
async def test_happy_path_returns_markdown(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """A normal stream of TEXT + DATAFRAME components renders as Markdown."""

    async def _impl(_ctx: Any, _q: str) -> AsyncIterator[UiComponent]:
        yield make_text_component("here are the rows you asked for")
        yield make_dataframe_component(
            columns=["id", "name"],
            rows=[{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}],
        )

    stub = agent_stub_factory(_impl)
    _build_agent_patch(monkeypatch, stub)

    result = await query_database.query_database_impl(test_config, "select * from users")

    assert "| id | name |" in result
    assert "| 1 | alice |" in result
    assert "| 2 | bob |" in result
    assert "here are the rows you asked for" in result


@pytest.mark.asyncio
async def test_concurrent_first_calls_build_once(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """Two concurrent cold-start calls must end up with exactly one build.

    With the current sync ``build_agent`` and sync ``_agent_for``, the
    check-then-set has no ``await`` between branches and is therefore atomic
    inside a single event loop — the test passes naturally today. If a future
    refactor makes ``build_agent`` awaitable without adding an ``asyncio.Lock``,
    this test will fail and surface the race.
    """
    stub = agent_stub_factory(_empty_send_message_impl())
    build_mock = _build_agent_patch(monkeypatch, stub)

    await asyncio.gather(
        query_database.query_database_impl(test_config, "q1"),
        query_database.query_database_impl(test_config, "q2"),
    )

    assert build_mock.call_count == 1


@pytest.mark.asyncio
async def test_send_message_generator_is_closed_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    test_config: Config,
    agent_stub_factory: Any,
) -> None:
    """When the agent's generator raises mid-stream, its cleanup must run.

    Verifies the async generator's ``finally`` clause (the moral equivalent of
    ``aclose``) executes when the inner stream raises. Resource cleanup —
    cursor close, transaction abort, file handle release — typically lives in
    such ``finally`` clauses, so this is the load-bearing contract.
    """
    cleanup_calls: list[str] = []

    async def _impl(_ctx: Any, _q: str) -> AsyncIterator[UiComponent]:
        try:
            yield make_text_component("partial answer")
            raise RuntimeError("agent blew up mid-stream")
        finally:
            cleanup_calls.append("closed")

    stub = agent_stub_factory(_impl)
    _build_agent_patch(monkeypatch, stub)

    with pytest.raises(RuntimeError, match="query_database failed"):
        await query_database.query_database_impl(test_config, "select 1")

    assert cleanup_calls == ["closed"]
