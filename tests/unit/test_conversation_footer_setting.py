# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``agent.show_conversation_footer`` keeps the conversation id out of ``query_database`` text.

The agent-run failure cases drive the real vendored ``Agent`` through the
server tool, so its catch-all handler builds the error card and appends the
``"\\n\\nConversation ID: <id>"`` tail itself; a later re-lift that changes that
tail fails here instead of leaking the id back into the text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

import sqllens.server as server_module
from sqllens.config import AgentRuntimeConfig, Config
from sqllens.server import build_server
from sqllens.tools import query_database as qd
from sqllens.tools.query_database import AgentRunError

from ._config_builders import build_test_config
from .test_query_database_tool_history import _TOOL_PROMPT, _agent, _GatedRunSql
from .test_server import _model_info, _query_database_fn, _StubCtx

pytestmark = pytest.mark.asyncio

_CONV = "conv-footer-1"
_FOOTER = (
    f"\n\n_Conversation ID: `{_CONV}` — pass it back as the "
    f"`conversation_id` argument to continue this conversation._"
)
_TAIL = f"\n\nConversation ID: {_CONV}"
_CARD = "An unexpected error occurred while processing your message. Please try again."
_PREFIX = "Error executing tool query_database: SQL execution error: "


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    qd._CONVERSATION_QUEUES.clear()
    yield
    qd._CONVERSATION_QUEUES.clear()


def _cfg(tmp_path: Path, *, footer: bool, details: bool = False) -> Config:
    return build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=details, show_conversation_footer=footer),
    )


def _raising_execute(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("tool registry exploded")


def _failing_real_agent(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real ``Agent`` whose tool execution raises, served to the server tool."""
    agent, _messages, _store = _agent(_GatedRunSql())
    agent.tool_registry.execute = _raising_execute  # type: ignore[method-assign]

    async def _get_agent(_cfg: Config) -> Any:
        return agent

    monkeypatch.setattr(qd, "get_agent", _get_agent)
    return agent


def _patch_impl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returns: str | None = None,
    raises: Exception | None = None,
) -> None:
    """Replace the impl with a stub that returns ``returns`` as the answer or raises."""

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        if raises is not None:
            raise raises
        return returns, [], None, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)


async def _call(cfg: Config, question: str = f"q, {_TOOL_PROMPT}") -> Any:
    return await _query_database_fn(build_server(cfg))(
        question, _StubCtx(), conversation_id=_CONV
    )


# --- success path ---------------------------------------------------------------


@pytest.mark.parametrize("footer", [True, False])
async def test_success_footer_follows_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, footer: bool
) -> None:
    _patch_impl(monkeypatch, returns="the answer")
    cfg = _cfg(tmp_path, footer=footer)

    result = await _call(cfg, "rows?")

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.content[0].text == ("the answer" + _FOOTER if footer else "the answer")
    assert result.meta == {
        "sqllens/conversation": {"conversation_id": _CONV},
        "sqllens/model_info": _model_info(cfg),
    }


async def test_success_footer_off_keeps_ids_the_model_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = f"Your conversation is {_CONV}.{_TAIL}"

    _patch_impl(monkeypatch, returns=answer)

    result = await _call(_cfg(tmp_path, footer=False), "rows?")

    assert result.content[0].text == answer


# --- agent-run failure, real vendored Agent ---------------------------------------


async def test_footer_on_details_off_failure_is_raised_with_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_real_agent(monkeypatch)

    with pytest.raises(AgentRunError) as excinfo:
        await _call(_cfg(tmp_path, footer=True))

    assert str(excinfo.value) == f"SQL execution error: {_CARD}{_TAIL}"
    assert excinfo.value.agent_trace is None


async def test_footer_on_details_on_failure_keeps_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_real_agent(monkeypatch)

    result = await _call(_cfg(tmp_path, footer=True, details=True))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.content[0].text == f"{_PREFIX}{_CARD}{_TAIL}"
    assert set(result.meta) == {"sqllens/conversation", "sqllens/agent_trace"}
    assert result.meta["sqllens/conversation"] == {"conversation_id": _CONV}


@pytest.mark.parametrize("details", [False, True])
async def test_footer_off_failure_is_returned_without_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, details: bool
) -> None:
    _failing_real_agent(monkeypatch)

    result = await _call(_cfg(tmp_path, footer=False, details=details))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.content[0].text == f"{_PREFIX}{_CARD}"
    assert _CONV not in result.content[0].text
    expected_keys = {"sqllens/conversation"} | ({"sqllens/agent_trace"} if details else set())
    assert set(result.meta) == expected_keys
    assert result.meta["sqllens/conversation"] == {"conversation_id": _CONV}


async def test_footer_off_memory_unavailable_message_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _messages, _store = _agent(_GatedRunSql())
    agent.agent_memory.open_error = RuntimeError("chroma could not open")

    async def _get_agent(_cfg: Config) -> Any:
        return agent

    monkeypatch.setattr(qd, "get_agent", _get_agent)

    result = await _call(_cfg(tmp_path, footer=False), "plain question")

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.content[0].text == (
        f"Error executing tool query_database: {qd._MEMORY_UNAVAILABLE_MESSAGE}"
    )
    assert result.meta == {"sqllens/conversation": {"conversation_id": _CONV}}


async def test_footer_off_keeps_id_that_is_not_the_trailing_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = f"SQL execution error: failed{_TAIL} (see logs)"

    _patch_impl(monkeypatch, raises=AgentRunError(message, agent_trace=None))

    result = await _call(_cfg(tmp_path, footer=False), "rows?")

    assert result.isError is True
    assert result.content[0].text == f"Error executing tool query_database: {message}"


async def test_footer_off_non_agent_run_error_is_still_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_impl(monkeypatch, raises=RuntimeError(qd._INTERNAL_ERROR_MESSAGE))

    with pytest.raises(RuntimeError) as excinfo:
        await _call(_cfg(tmp_path, footer=False), "rows?")

    assert not isinstance(excinfo.value, AgentRunError)
    assert str(excinfo.value) == qd._INTERNAL_ERROR_MESSAGE
