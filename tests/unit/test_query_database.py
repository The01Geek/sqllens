# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for ``sqllens.tools.query_database``.

These tests pin the tool wrapper's behavior around the lazy-built ``_AGENT``
singleton: when it builds, when it reuses, when it surfaces errors, and how
it cleans up the underlying ``send_message`` async generator. The agent
itself is stubbed via ``agent_stub_factory`` (see ``tests/unit/conftest.py``)
so no LLM key or ChromaDB download is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sqllens.config import Config
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import query_database_impl

from ._agent_stubs import make_dataframe, make_status_card, make_text_component
from ._config_builders import build_test_config


@pytest.mark.asyncio
async def test_first_call_builds_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """First call goes through ``build_agent``."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("hello")])
    calls: list[Config] = []

    def fake_build_agent(c: Config):
        calls.append(c)
        return stub

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "question?")

    assert calls == [cfg]


@pytest.mark.asyncio
async def test_second_call_reuses_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Subsequent calls reuse the cached agent."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("answer")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "q1")
    await query_database_impl(cfg, "q2")

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_singleton_ignores_changed_cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Documents current behavior: a second call with a fresh ``Config`` is ignored.

    This is a known limitation tracked alongside the singleton race (see
    #72's out-of-scope section). The test pins the current behavior so that
    any future fix has a clear regression target.
    """
    cfg_a = build_test_config(persist_dir=tmp_path / "chroma")
    cfg_b = build_test_config(persist_dir=tmp_path / "alt")
    seen: list[Config] = []

    def fake_build_agent(c: Config):
        seen.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg_a, "q")
    await query_database_impl(cfg_b, "q")

    assert seen == [cfg_a]  # cfg_b was silently dropped


@pytest.mark.asyncio
async def test_build_agent_raises_leaves_singleton_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """If ``build_agent`` raises, ``_AGENT`` stays None so a retry can succeed."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def flaky_build_agent(c: Config):
        builds.append(c)
        if len(builds) == 1:
            raise RuntimeError("boom on first build")
        return agent_stub_factory([make_text_component("recovered")])

    monkeypatch.setattr(query_database_module, "build_agent", flaky_build_agent)

    with pytest.raises(RuntimeError, match="boom on first build"):
        await query_database_impl(cfg, "q1")

    assert query_database_module._AGENT is None
    result = await query_database_impl(cfg, "q2")
    assert "recovered" in result
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_send_message_raises_surfaces_as_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Errors from ``send_message`` are wrapped in ``RuntimeError`` with chained cause."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    original = ValueError("LLM exploded")
    stub = agent_stub_factory(raise_exc=original)
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError, match="query_database failed: LLM exploded") as excinfo:
        await query_database_impl(cfg, "q")

    assert excinfo.value.__cause__ is original


@pytest.mark.asyncio
async def test_is_error_status_card_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A STATUS_CARD with status='error' surfaces as a RuntimeError to the MCP client."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [make_status_card(description="schema introspection failed")]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError, match="schema introspection failed"):
        await query_database_impl(cfg, "q")


@pytest.mark.asyncio
async def test_happy_path_returns_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A normal TEXT + DATAFRAME stream collapses to a Markdown string."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            make_text_component("Here are the results:"),
            make_dataframe([{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]),
        ]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    result = await query_database_impl(cfg, "list users")

    assert "Here are the results:" in result
    assert "Alice" in result
    assert "| name | age |" in result


@pytest.mark.asyncio
async def test_concurrent_first_calls_build_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Singleton invariant holds under ``asyncio.gather`` of two cold-start calls.

    The current ``_agent_for`` is synchronous (no ``await`` between the
    ``if _AGENT is None`` check and the assignment), so two awaits started
    via ``asyncio.gather`` cannot interleave inside that critical section
    on a single event loop. If a future refactor inserts an ``await``
    between the check and the set without protecting it with a lock, this
    assertion is the regression signal.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await asyncio.gather(
        query_database_impl(cfg, "q1"),
        query_database_impl(cfg, "q2"),
    )

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_send_message_generator_is_closed_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """The agent's generator's cleanup block runs when ``send_message`` raises.

    When the async generator raises during ``__anext__``, Python's own
    exception-propagation machinery unwinds the generator frame and runs
    its ``finally`` (or ``aclose``-equivalent) block before the exception
    reaches the wrapper's ``except``. The wrapper relies on this — it does
    not invoke ``aclose()`` explicitly. A future refactor that defers
    iteration (e.g. ``while True: __anext__()`` without a surrounding
    cleanup) would leak the agent's resources on the error path; this
    test pins the current cleanup-on-raise guarantee.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(raise_exc=ValueError("midstream failure"))
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError):
        await query_database_impl(cfg, "q")

    assert stub.cleanup_ran is True
