# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for #247: tool calls cut off by the LLM output-token limit.

With the framework's 512-token fallback, a ``run_sql`` call carrying a long
statement was cut off mid-arguments, parsed as ``{"_raw": None}``, replayed to
the model in the history, and repeated until ``max_tool_iterations``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from sqllens.agent.core import AgentConfig
from sqllens.agent.core.agent.agent import (
    ARGUMENT_FAILURE_STOP_MESSAGE,
    MAX_CONSECUTIVE_ARGUMENT_FAILURES,
    Agent,
)
from sqllens.agent.core.agent.config import UiFeature, UiFeatures
from sqllens.agent.core.components import UiComponent
from sqllens.agent.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk
from sqllens.agent.core.registry import TRUNCATED_TOOL_CALL_MESSAGE, ToolRegistry
from sqllens.agent.core.tool import Tool, ToolCall, ToolContext, ToolResult
from sqllens.agent.core.user import RequestContext, User
from sqllens.agent.factory import DEFAULT_USER_GROUP, build_agent
from sqllens.agent.integrations.anthropic.llm import AnthropicLlmService
from sqllens.config import LLMConfig
from sqllens.tools._format import components_to_blocks

from ._agent_stubs import StubAgentMemory
from ._config_builders import build_test_config

# ---------------------------------------------------------------------------
# Config + factory wiring
# ---------------------------------------------------------------------------


def test_llm_max_tokens_default_is_well_above_framework_fallback() -> None:
    assert LLMConfig().max_tokens == 8192


def test_llm_max_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqllens.config import Config

    monkeypatch.setenv("SQLLENS_DATABASE__URL", "sqlite:///:memory:")
    monkeypatch.setenv("SQLLENS_LLM__MAX_TOKENS", "20000")
    assert Config().llm.max_tokens == 20000


@pytest.mark.parametrize("bad", [0, -1, 128_001])
def test_llm_max_tokens_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError):
        LLMConfig(max_tokens=bad)


def test_max_tokens_flows_through_factory(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    cfg.llm = LLMConfig(api_key=SecretStr("sk-ant-test"), max_tokens=12345)
    agent = build_agent(cfg)
    assert agent.config.max_tokens == 12345


# ---------------------------------------------------------------------------
# Anthropic response parsing
# ---------------------------------------------------------------------------


_USER = User(id="u", email="u@local", group_memberships=[DEFAULT_USER_GROUP])


def _service() -> AnthropicLlmService:
    return AnthropicLlmService(model="m", api_key="sk-ant-test")


def _msg(blocks: list[dict[str, Any]], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def _tool_use(input_: Any, name: str = "run_sql", id_: str = "t1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def test_payload_uses_request_max_tokens() -> None:
    request = LlmRequest(messages=[], user=_USER, max_tokens=8192)
    assert _service()._build_payload(request)["max_tokens"] == 8192


def test_complete_tool_call_is_unchanged() -> None:
    _, calls = _service()._parse_message_content(
        _msg([_tool_use({"sql": "SELECT 1"})], "tool_use")
    )
    assert calls[0].arguments == {"sql": "SELECT 1"}
    assert calls[0].truncated is False


def test_missing_input_never_becomes_raw_none_placeholder() -> None:
    _, calls = _service()._parse_message_content(_msg([_tool_use(None)], "tool_use"))
    assert calls[0].arguments == {}
    assert "_raw" not in calls[0].arguments


def test_empty_dict_input_is_kept() -> None:
    _, calls = _service()._parse_message_content(_msg([_tool_use({})], "tool_use"))
    assert calls[0].arguments == {}


@pytest.mark.parametrize("partial", [None, {}, {"sql": "SELECT a, b FROM orders WHERE"}])
def test_max_tokens_marks_last_tool_call_truncated(partial: Any) -> None:
    """Even a partially parsed input must not run — it may be half a statement."""
    text, calls = _service()._parse_message_content(
        _msg(
            [
                {"type": "text", "text": "Running the query."},
                _tool_use({"question": "q"}, name="search", id_="t0"),
                _tool_use(partial, id_="t1"),
            ],
            "max_tokens",
        )
    )
    assert text == "Running the query."
    assert calls[0].truncated is False
    assert calls[0].arguments == {"question": "q"}
    assert calls[1].truncated is True
    assert calls[1].arguments == {}
    assert calls[1].id == "t1"


def test_max_tokens_without_tool_call_marks_nothing() -> None:
    text, calls = _service()._parse_message_content(
        _msg([{"type": "text", "text": "partial answer"}], "max_tokens")
    )
    assert text == "partial answer"
    assert calls == []


# ---------------------------------------------------------------------------
# Registry + agent loop
# ---------------------------------------------------------------------------


class _SqlArgs(BaseModel):
    sql: str


class _FakeRunSql(Tool[_SqlArgs]):
    def __init__(self) -> None:
        self.executed: list[str] = []

    @property
    def name(self) -> str:
        return "run_sql"

    @property
    def description(self) -> str:
        return "Run SQL"

    def get_args_schema(self) -> type[_SqlArgs]:
        return _SqlArgs

    async def execute(self, context: ToolContext, args: _SqlArgs) -> ToolResult:
        self.executed.append(args.sql)
        return ToolResult(success=True, result_for_llm="1 row")


class _ScriptedLlm(LlmService):
    """Replays a fixed list of responses, repeating the last one forever."""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.requests: list[LlmRequest] = []

    def _next(self) -> LlmResponse:
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return self._next()

    async def stream_request(self, request: LlmRequest) -> AsyncGenerator[LlmStreamChunk, None]:
        self.requests.append(request)
        resp = self._next()
        yield LlmStreamChunk(
            content=resp.content,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )

    async def validate_tools(self, tools: list[Any]) -> list[str]:
        return []


class _User:
    async def resolve_user(self, request_context: RequestContext) -> User:
        return _USER


def _agent(llm: LlmService, tool: Tool[Any], max_iterations: int = 20) -> Agent:
    registry = ToolRegistry()
    registry.register_local_tool(tool, access_groups=[DEFAULT_USER_GROUP])
    # Same UI-feature grant as factory.build_agent, so tool status cards (which the
    # MCP layer uses to classify a run as failed) are emitted exactly as in production.
    ui_features = UiFeatures()
    ui_features.register_feature(
        UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS,
        [
            *ui_features.feature_group_access.get(UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS, []),
            DEFAULT_USER_GROUP,
        ],
    )
    return Agent(
        llm_service=llm,
        tool_registry=registry,
        user_resolver=_User(),  # type: ignore[arg-type]
        agent_memory=StubAgentMemory(),
        config=AgentConfig(
            max_tool_iterations=max_iterations, max_tokens=4096, ui_features=ui_features
        ),
    )


async def _run(agent: Agent) -> list[UiComponent]:
    ctx = RequestContext(headers={}, cookies={}, metadata={})
    return [c async for c in agent.send_message(ctx, "sales by month")]


def _tool_msgs(llm: _ScriptedLlm) -> list[str]:
    last = llm.requests[-1]
    return [m.content for m in last.messages if m.role == "tool"]


@pytest.mark.asyncio
async def test_registry_refuses_truncated_call_without_executing() -> None:
    tool = _FakeRunSql()
    registry = ToolRegistry()
    registry.register_local_tool(tool, access_groups=[DEFAULT_USER_GROUP])
    ctx = ToolContext(
        user=_USER,
        conversation_id="c",
        request_id="r",
        agent_memory=StubAgentMemory(),
    )
    result = await registry.execute(
        ToolCall(id="t", name="run_sql", arguments={"sql": "SELECT"}, truncated=True), ctx
    )
    assert result.success is False
    assert result.error == TRUNCATED_TOOL_CALL_MESSAGE.format(tool="run_sql")
    assert tool.executed == []


@pytest.mark.asyncio
async def test_truncated_call_is_explained_and_recovered() -> None:
    """One truncated call, then a good one: the model is told why, then succeeds."""
    tool = _FakeRunSql()
    llm = _ScriptedLlm(
        [
            LlmResponse(
                tool_calls=[ToolCall(id="a", name="run_sql", arguments={}, truncated=True)],
                finish_reason="max_tokens",
            ),
            LlmResponse(
                tool_calls=[ToolCall(id="b", name="run_sql", arguments={"sql": "SELECT 1"})]
            ),
            LlmResponse(content="Done.", finish_reason="end_turn"),
        ]
    )
    await _run(_agent(llm, tool))

    assert tool.executed == ["SELECT 1"]
    assert llm.calls == 3
    assert TRUNCATED_TOOL_CALL_MESSAGE.format(tool="run_sql") in _tool_msgs(llm)
    # The history replayed to the model never carries a _raw placeholder.
    for msg in llm.requests[-1].messages:
        for tc in msg.tool_calls or []:
            assert "_raw" not in tc.arguments
    assert all(r.max_tokens == 4096 for r in llm.requests)


@pytest.mark.asyncio
async def test_repeated_invalid_calls_stop_early_as_error() -> None:
    """The #247 loop: the same broken call forever must stop well before the cap."""
    tool = _FakeRunSql()
    llm = _ScriptedLlm([LlmResponse(tool_calls=[ToolCall(id="x", name="run_sql", arguments={})])])
    components = await _run(_agent(llm, tool, max_iterations=100))

    assert llm.calls == MAX_CONSECUTIVE_ARGUMENT_FAILURES
    assert tool.executed == []
    messages = [getattr(c.rich_component, "message", None) for c in components]
    assert ARGUMENT_FAILURE_STOP_MESSAGE in messages
    assert "Tool limit reached" not in messages
    _, is_error, _, _, _ = components_to_blocks(components)
    assert is_error is True


@pytest.mark.asyncio
async def test_repeated_truncated_calls_stop_early() -> None:
    tool = _FakeRunSql()
    llm = _ScriptedLlm(
        [
            LlmResponse(
                tool_calls=[ToolCall(id="x", name="run_sql", arguments={}, truncated=True)],
                finish_reason="max_tokens",
            )
        ]
    )
    await _run(_agent(llm, tool, max_iterations=100))
    assert llm.calls == MAX_CONSECUTIVE_ARGUMENT_FAILURES
    assert tool.executed == []


@pytest.mark.asyncio
async def test_failure_streak_resets_after_a_valid_call() -> None:
    """Non-consecutive argument failures must not trip the guard."""
    tool = _FakeRunSql()
    bad = LlmResponse(tool_calls=[ToolCall(id="x", name="run_sql", arguments={})])
    good = LlmResponse(tool_calls=[ToolCall(id="y", name="run_sql", arguments={"sql": "SELECT 1"})])
    llm = _ScriptedLlm([bad, bad, good, bad, bad, good, LlmResponse(content="Done.")])
    components = await _run(_agent(llm, tool))

    assert tool.executed == ["SELECT 1", "SELECT 1"]
    assert llm.calls == 7
    messages = [getattr(c.rich_component, "message", None) for c in components]
    assert ARGUMENT_FAILURE_STOP_MESSAGE not in messages
