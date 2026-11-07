# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""A memory store that cannot be opened surfaces as a structured query error (issue #323)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolResult

from sqllens.agent.integrations.chromadb import agent_memory as chroma_memory
from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.config import AgentRuntimeConfig, Config
from sqllens.server import build_server
from sqllens.tools import _agent as agent_module
from sqllens.tools import query_database as qd
from sqllens.tools.query_database import (
    _MEMORY_UNAVAILABLE_MESSAGE,
    AgentRunError,
    query_database_impl_with_widgets,
)

from ._agent_stubs import make_dataframe, make_text_component, make_tool_cards
from ._config_builders import build_test_config
from .test_server import _query_database_fn, _StubCtx

EXPECTED_MESSAGE = (
    "Memory store unavailable: the vector memory store could not be opened, so the "
    "answer would have been produced without saved memory. Check that "
    "memory.persist_dir and the files in it are writable by the server process, "
    "then check the server logs."
)
CHROMA_TEXT = "error returned from database: (code: 8) attempt to write a readonly database"


# --- ChromaAgentMemory.open_error ----------------------------------------------------


def _memory(tmp_path: Path) -> ChromaAgentMemory:
    return ChromaAgentMemory(persist_directory=str(tmp_path / "chroma"))


def test_failed_open_is_recorded_and_reraised(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    assert memory.open_error is None
    exc = RuntimeError(CHROMA_TEXT)
    with (
        patch("chromadb.PersistentClient", side_effect=exc),
        pytest.raises(RuntimeError, match="readonly database"),
    ):
        memory._get_client()
    assert memory.open_error is exc


def test_later_failed_open_replaces_record(tmp_path: Path) -> None:
    """Chroma alternates exceptions across opens; the latest one is recorded."""
    memory = _memory(tmp_path)
    second = AttributeError("'RustBindingsAPI' object has no attribute 'bindings'")
    with patch("chromadb.PersistentClient", side_effect=[RuntimeError(CHROMA_TEXT), second]):
        for _ in range(2):
            with pytest.raises((RuntimeError, AttributeError)):
                memory._get_client()
    assert memory.open_error is second


def test_failed_collection_state_read_is_recorded(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    exc = OSError("permission denied")
    with (
        patch("chromadb.PersistentClient", return_value=MagicMock()),
        patch.object(chroma_memory, "collection_state", side_effect=exc),
        pytest.raises(OSError),
    ):
        memory._get_client()
    assert memory.open_error is exc
    assert memory._client is None


def test_successful_open_clears_record(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    client = MagicMock()
    with (
        patch("chromadb.PersistentClient", side_effect=[RuntimeError(CHROMA_TEXT), client]),
        patch.object(chroma_memory, "collection_state", return_value=None),
    ):
        with pytest.raises(RuntimeError):
            memory._get_client()
        assert memory.open_error is not None
        assert memory._get_client() is client
    assert memory.open_error is None


def test_late_failed_open_after_concurrent_success_reads_absent(tmp_path: Path) -> None:
    """A failed open finishing after another thread's successful open does not stick."""
    memory = _memory(tmp_path)
    memory._client = MagicMock()
    memory._open_error = RuntimeError(CHROMA_TEXT)
    assert memory.open_error is None


# --- query_database_impl_with_widgets --------------------------------------------------


def _failed_search_cards() -> list:
    return make_tool_cards(
        "search_saved_correct_tool_uses", {"question": "q"}, ok=False, error=CHROMA_TEXT
    )


def _turn_with_later_data_success() -> list:
    return [
        *_failed_search_cards(),
        *make_tool_cards("run_sql", {"sql": "SELECT count(*) FROM orders"}, ok=True),
        make_dataframe([{"count": 42}]),
        make_text_component("42 orders"),
    ]


def _turn_ending_in_failed_search() -> list:
    return [*_failed_search_cards(), make_text_component("Sorry, memory failed.")]


@pytest.mark.asyncio
@pytest.mark.parametrize("show_details", [False, True])
@pytest.mark.parametrize(
    "components", [_turn_with_later_data_success, _turn_ending_in_failed_search]
)
async def test_recorded_open_error_raises_memory_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    show_details: bool,
    components,
) -> None:
    persist_dir = tmp_path / "chroma"
    cfg = build_test_config(
        persist_dir=persist_dir, agent=AgentRuntimeConfig(show_details=show_details)
    )
    stub = agent_stub_factory(components())
    stub.agent_memory.open_error = RuntimeError(f"{CHROMA_TEXT} at {persist_dir}")
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(AgentRunError) as excinfo:
        await query_database_impl_with_widgets(cfg, "how many orders?")

    message = str(excinfo.value)
    assert message == EXPECTED_MESSAGE == _MEMORY_UNAVAILABLE_MESSAGE
    assert str(persist_dir) not in message
    assert CHROMA_TEXT not in message
    if show_details:
        trace = excinfo.value.agent_trace
        assert trace is not None
        failed = [s for s in trace["steps"] if s["status"] != "ok"]
        assert failed and all(CHROMA_TEXT in (s.get("error") or "") for s in failed)
    else:
        assert excinfo.value.agent_trace is None


@pytest.mark.asyncio
async def test_cleared_open_error_returns_normal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    """Recovery without restart: once the record is clear the answer comes back."""
    from .test_query_database import _memory_card

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            _memory_card(hit_count=2, top_similarity=0.9),
            *make_tool_cards("run_sql", {"sql": "SELECT 1"}, ok=True),
            make_text_component("done"),
        ]
    )
    stub.agent_memory.open_error = None
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, _blocks, _qi, memory_info, _trace = await query_database_impl_with_widgets(cfg, "q")
    assert "done" in markdown
    assert memory_info is not None


@pytest.mark.asyncio
async def test_memory_without_attribute_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(_turn_with_later_data_success())
    assert not hasattr(stub.agent_memory, "open_error")
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, *_ = await query_database_impl_with_widgets(cfg, "q")
    assert "42 orders" in markdown


@pytest.mark.asyncio
async def test_turn_without_model_call_raises_only_when_failure_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    """A workflow-handled message (no model, no open) reports an earlier recorded failure."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("help text")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, *_ = await query_database_impl_with_widgets(cfg, "help")
    assert "help text" in markdown

    stub.agent_memory.open_error = RuntimeError(CHROMA_TEXT)
    with pytest.raises(AgentRunError, match=r"^Memory store unavailable: "):
        await query_database_impl_with_widgets(cfg, "help")


# --- MCP result -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("show_details", [False, True])
async def test_mcp_result_is_error_with_memory_unavailable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, show_details: bool
) -> None:
    """Real agent turn (scripted model) runs to completion, then the call errors."""
    from sqllens.agent.core.llm import LlmResponse

    from .test_truncated_tool_calls import _agent, _FakeRunSql, _ScriptedLlm

    llm = _ScriptedLlm([LlmResponse(content="An answer without memory.")])
    agent = _agent(llm, _FakeRunSql())
    agent.agent_memory.open_error = RuntimeError(CHROMA_TEXT)  # type: ignore[attr-defined]

    async def _fake_get_agent(_cfg: Config):
        return agent

    monkeypatch.setattr(qd, "get_agent", _fake_get_agent)
    mcp = build_server(
        build_test_config(
            persist_dir=tmp_path / "chroma",
            agent=AgentRuntimeConfig(show_details=show_details),
        )
    )
    query_database = _query_database_fn(mcp)

    if show_details:
        result = await query_database("q", _StubCtx())
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.content[0].text == f"Error executing tool query_database: {EXPECTED_MESSAGE}"
        assert "sqllens/agent_trace" in (result.meta or {})
    else:
        # Without a trace the server re-raises; FastMCP adds the same prefix.
        with pytest.raises(AgentRunError, match=r"^Memory store unavailable: "):
            await query_database("q", _StubCtx())
    assert len(llm.requests) == 1


# --- server-side logging and a real failed open -----------------------------------------


@pytest.mark.asyncio
async def test_open_failure_cause_is_logged_server_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The client message omits the Chroma text, so the server log must carry it."""
    persist_dir = tmp_path / "chroma"
    cfg = build_test_config(persist_dir=persist_dir)
    stub = agent_stub_factory(_turn_ending_in_failed_search())
    exc = RuntimeError(f"{CHROMA_TEXT} at {persist_dir}")
    stub.agent_memory.open_error = exc
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with caplog.at_level("WARNING", logger="sqllens.tools.query_database"):
        with pytest.raises(AgentRunError):
            await query_database_impl_with_widgets(cfg, "q")

    records = [r for r in caplog.records if r.exc_info and r.exc_info[1] is exc]
    assert records, "the open failure was not logged with its exception"
    assert CHROMA_TEXT in caplog.text
    assert str(persist_dir) in caplog.text


@pytest.mark.asyncio
async def test_real_memory_failed_open_fails_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    """A real ChromaAgentMemory whose open failed drives the memory-unavailable error."""
    persist_dir = tmp_path / "chroma"
    cfg = build_test_config(persist_dir=persist_dir)
    memory = ChromaAgentMemory(persist_directory=str(persist_dir))
    with (
        patch("chromadb.PersistentClient", side_effect=RuntimeError(CHROMA_TEXT)),
        pytest.raises(RuntimeError),
    ):
        memory._get_client()
    stub = agent_stub_factory(_turn_with_later_data_success())
    stub.agent_memory = memory
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(AgentRunError) as excinfo:
        await query_database_impl_with_widgets(cfg, "how many orders?")
    assert str(excinfo.value) == EXPECTED_MESSAGE


@pytest.mark.asyncio
async def test_memory_unavailable_turn_is_rolled_back_from_history(tmp_path: Path) -> None:
    """The failed turn's answer, produced without memory, is not kept for later turns."""
    from sqllens.agent.core.agent import Agent
    from sqllens.agent.core.llm import LlmResponse
    from sqllens.agent.factory import _StaticUserResolver
    from sqllens.conversation_store import BoundedConversationStore

    from .test_truncated_tool_calls import _agent, _FakeRunSql, _ScriptedLlm

    llm = _ScriptedLlm(
        [LlmResponse(content="first answer"), LlmResponse(content="answer without memory")]
    )
    template = _agent(llm, _FakeRunSql())
    store = BoundedConversationStore()
    agent = Agent(
        llm_service=llm,
        tool_registry=template.tool_registry,
        user_resolver=_StaticUserResolver(),
        agent_memory=template.agent_memory,
        conversation_store=store,
        config=template.config,
    )
    cfg = build_test_config(persist_dir=tmp_path / "chroma")

    async def history() -> list[str]:
        user = await _StaticUserResolver().resolve_user(None)
        conversation = await store.get_conversation("c1", user)
        return [] if conversation is None else [m.content for m in conversation.messages]

    await query_database_impl_with_widgets(cfg, "first", conversation_id="c1", agent=agent)
    before = await history()
    assert before

    agent.agent_memory.open_error = RuntimeError(CHROMA_TEXT)  # type: ignore[attr-defined]
    with pytest.raises(AgentRunError, match=r"^Memory store unavailable: "):
        await query_database_impl_with_widgets(cfg, "second", conversation_id="c1", agent=agent)
    assert await history() == before


@pytest.mark.asyncio
async def test_format_failure_with_open_error_still_reports_memory_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    """The memory-unavailable category wins over a formatter failure in the same turn."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(_turn_ending_in_failed_search())
    stub.agent_memory.open_error = RuntimeError(CHROMA_TEXT)
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    def _boom(*_a, **_k):
        raise ValueError("formatter broke")

    monkeypatch.setattr(qd, "components_to_blocks", _boom)
    with pytest.raises(AgentRunError) as excinfo:
        await query_database_impl_with_widgets(cfg, "q")
    assert str(excinfo.value) == EXPECTED_MESSAGE
