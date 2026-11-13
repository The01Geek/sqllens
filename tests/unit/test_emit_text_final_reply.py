# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The final reply is hidden once an ``emit_text`` call succeeds in a request.

Drives the real ``Agent`` behind the registered ``query_database`` tool with a
scripted model, the real ``EmitTextTool`` and a fake ``run_sql``, so the agent
loop, the block builder and the conversation store all run for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import CallToolResult

from sqllens.agent.core import AgentConfig
from sqllens.agent.core.agent.agent import Agent
from sqllens.agent.core.agent.config import UiFeature, UiFeatures
from sqllens.agent.core.llm import LlmResponse
from sqllens.agent.core.registry import ToolRegistry
from sqllens.agent.core.tool import ToolCall, ToolContext, ToolResult
from sqllens.agent.factory import DEFAULT_USER_GROUP
from sqllens.agent.tools.emit_text import EmitTextTool
from sqllens.config import Config
from sqllens.server import build_server
from sqllens.tools import query_database as qd

from ._agent_stubs import StubAgentMemory, make_dataframe
from ._config_builders import build_test_config
from .test_server import _query_database_fn, _StubCtx
from .test_truncated_tool_calls import _FakeRunSql, _ScriptedLlm, _SqlArgs, _User

pytestmark = pytest.mark.asyncio

_PROSE = "There were 42 orders last month."


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    qd._CONVERSATION_QUEUES.clear()
    yield
    qd._CONVERSATION_QUEUES.clear()


def _emit(text: str, id_: str) -> LlmResponse:
    return LlmResponse(tool_calls=[ToolCall(id=id_, name="emit_text", arguments={"text": text})])


def _sql(id_: str) -> LlmResponse:
    return LlmResponse(
        tool_calls=[ToolCall(id=id_, name="run_sql", arguments={"sql": "SELECT 1"})]
    )


class _OneRowRunSql(_FakeRunSql):
    """``run_sql`` returning a one-row result the block builder shows as text."""

    async def execute(self, context: ToolContext, args: _SqlArgs) -> ToolResult:
        self.executed.append(args.sql)
        return ToolResult(
            success=True,
            result_for_llm="1 row",
            ui_component=make_dataframe([{"orders": 42}]),
        )


def _query_database(
    responses: list[LlmResponse],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_sql: _FakeRunSql | None = None,
    max_tool_iterations: int = 20,
):  # type: ignore[no-untyped-def]
    llm = _ScriptedLlm(responses)
    registry = ToolRegistry()
    registry.register_local_tool(EmitTextTool(), access_groups=[DEFAULT_USER_GROUP])
    registry.register_local_tool(run_sql or _FakeRunSql(), access_groups=[DEFAULT_USER_GROUP])
    # Same UI-feature grant as factory.build_agent, so a failed tool's error
    # status card reaches the block builder exactly as in production.
    ui_features = UiFeatures()
    ui_features.register_feature(
        UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS,
        [
            *ui_features.feature_group_access.get(UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS, []),
            DEFAULT_USER_GROUP,
        ],
    )
    agent = Agent(
        llm_service=llm,
        tool_registry=registry,
        user_resolver=_User(),  # type: ignore[arg-type]
        agent_memory=StubAgentMemory(),
        config=AgentConfig(
            max_tool_iterations=max_tool_iterations, max_tokens=4096, ui_features=ui_features
        ),
    )

    async def _fake_get_agent(_cfg: Config) -> Agent:
        return agent

    monkeypatch.setattr(qd, "get_agent", _fake_get_agent)
    mcp = build_server(build_test_config(persist_dir=tmp_path / "chroma"))
    return _query_database_fn(mcp), llm


def _texts(result: CallToolResult) -> tuple[list[str], str]:
    """The result's text blocks and its Markdown text content."""
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    blocks = result.meta["sqllens/blocks"]
    text_blocks = [b["text"] for b in blocks if b["type"] == "text"]
    markdown = "\n".join(c.text for c in result.content if c.type == "text")
    return text_blocks, markdown


@pytest.mark.parametrize(
    "final_reply",
    [_PROSE, "Last month saw 42 orders.", "Anything else?", ""],
    ids=["identical", "reworded", "unrelated", "empty"],
)
async def test_final_reply_hidden_after_successful_emit_text(
    final_reply: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_database, _ = _query_database(
        [_emit(_PROSE, "e1"), LlmResponse(content=final_reply)], tmp_path, monkeypatch
    )

    text_blocks, markdown = _texts(await query_database("orders?", _StubCtx()))

    assert text_blocks == [_PROSE]
    assert markdown.count(_PROSE) == 1
    if final_reply and final_reply != _PROSE:
        assert final_reply not in markdown


async def test_final_reply_shown_without_emit_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_database, _ = _query_database(
        [_sql("s1"), LlmResponse(content=_PROSE)], tmp_path, monkeypatch
    )

    text_blocks, markdown = _texts(await query_database("orders?", _StubCtx()))

    assert text_blocks == [_PROSE]
    assert _PROSE in markdown


async def test_final_reply_shown_after_failed_emit_text_and_recovered_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_database, _ = _query_database(
        [_emit("   ", "e1"), _sql("s1"), LlmResponse(content=_PROSE)], tmp_path, monkeypatch
    )

    text_blocks, markdown = _texts(await query_database("orders?", _StubCtx()))

    assert text_blocks == [_PROSE]
    assert _PROSE in markdown


async def test_repeated_emit_text_blocks_all_shown_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_database, _ = _query_database(
        [_emit(_PROSE, "e1"), _sql("s1"), _emit(_PROSE, "e2"), LlmResponse(content=_PROSE)],
        tmp_path,
        monkeypatch,
    )

    text_blocks, markdown = _texts(await query_database("orders?", _StubCtx()))

    assert text_blocks == [_PROSE, _PROSE]
    assert markdown.count(_PROSE) == 2


async def test_one_row_result_still_shown_in_order_when_final_reply_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_database, _ = _query_database(
        [_sql("s1"), _emit(_PROSE, "e1"), LlmResponse(content="Anything else?")],
        tmp_path,
        monkeypatch,
        run_sql=_OneRowRunSql(),
    )

    text_blocks, markdown = _texts(await query_database("orders?", _StubCtx()))

    assert len(text_blocks) == 2
    assert "42" in text_blocks[0]
    assert text_blocks[1] == _PROSE
    assert "Anything else?" not in markdown


async def test_iteration_limit_warning_shown_after_successful_emit_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scripted model repeats its last response, so it never stops calling tools.
    query_database, _ = _query_database(
        [_emit(_PROSE, "e1"), _sql("s1")],
        tmp_path,
        monkeypatch,
        max_tool_iterations=3,
    )

    text_blocks, _ = _texts(await query_database("orders?", _StubCtx()))

    assert text_blocks[0] == _PROSE
    assert len(text_blocks) == 2
    assert "Tool Execution Limit Reached" in text_blocks[1]


async def test_hidden_final_reply_stays_in_history_and_next_request_shows_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = "Summary the user never sees."
    follow_up = "Follow-up answer."
    query_database, llm = _query_database(
        [_emit(_PROSE, "e1"), LlmResponse(content=hidden), LlmResponse(content=follow_up)],
        tmp_path,
        monkeypatch,
    )

    first = await query_database("orders?", _StubCtx())
    text_blocks, markdown = _texts(first)
    assert text_blocks == [_PROSE]
    assert hidden not in markdown
    conversation_id = first.meta["sqllens/conversation"]["conversation_id"]

    # A successful emit_text in the earlier request does not hide this one's reply.
    second = await query_database("and before?", _StubCtx(), conversation_id=conversation_id)
    text_blocks, _ = _texts(second)
    assert text_blocks == [follow_up]

    history = llm.requests[-1].messages
    hidden_messages = [m for m in history if m.content == hidden]
    assert [m.role for m in hidden_messages] == ["assistant"]
    hidden_at = history.index(hidden_messages[0])
    assert history[hidden_at - 1].role == "tool"
    assert history[hidden_at + 1].role == "user"
