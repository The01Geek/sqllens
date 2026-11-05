# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""A stopped ``query_database`` call removes an unanswered ``tool_use``, or logs why not.

The real ``Agent`` saves the assistant message carrying its tool calls before
any tool runs, and saves the tool results only after the round finishes. A
call that stops in between leaves a ``tool_use`` with no ``tool_result``, which
the Anthropic API rejects on every later request in that conversation. These
tests drive the real agent through ``query_database_impl_with_widgets`` with a
``BoundedConversationStore`` and a stub Anthropic client that records each
request payload, so the next call's payload shows whether the API would accept
it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel

from sqllens.agent.core import AgentConfig
from sqllens.agent.core.agent.agent import Agent
from sqllens.agent.core.agent.config import UiFeature, UiFeatures
from sqllens.agent.core.registry import ToolRegistry
from sqllens.agent.core.tool import Tool, ToolContext, ToolResult
from sqllens.agent.factory import DEFAULT_USER_GROUP, _StaticUserResolver
from sqllens.agent.integrations.anthropic.llm import AnthropicLlmService
from sqllens.conversation_store import (
    BoundedConversationStore,
    ConversationOwnerMismatchError,
)
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import AgentRunError, query_database_impl_with_widgets

from ._agent_stubs import StubAgentMemory
from ._config_builders import build_test_config

_TOOL_PROMPT = "use the tool"
_CONVERSATION = "conv-c"


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    query_database_module._CONVERSATION_QUEUES.clear()
    yield
    query_database_module._CONVERSATION_QUEUES.clear()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if b.get("type") == "text")


class _Messages:
    """Stub ``client.messages``: asks for a tool when the user says so.

    A request whose last message is a plain user turn containing
    ``_TOOL_PROMPT`` gets a ``run_sql`` ``tool_use``; every other request gets a
    text answer. Each payload is recorded in ``requests``.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._tool_ids = 0

    async def create(self, **kwargs: Any) -> AnthropicMessage:
        self.requests.append(deepcopy(kwargs))
        last = kwargs["messages"][-1]
        wants_tool = last["role"] == "user" and _TOOL_PROMPT in _text_of(last["content"])
        if wants_tool:
            self._tool_ids += 1
            content: list[dict[str, Any]] = [
                {"type": "text", "text": "Running it."},
                {
                    "type": "tool_use",
                    "id": f"toolu_{self._tool_ids}",
                    "name": "run_sql",
                    "input": {"sql": "SELECT 1"},
                },
            ]
        else:
            content = [{"type": "text", "text": "Done."}]
        return AnthropicMessage.model_validate({
            "id": f"msg-{len(self.requests)}",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": "tool_use" if wants_tool else "end_turn",
            "stop_sequence": None,
            "type": "message",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })


class _SqlArgs(BaseModel):
    sql: str


class _GatedRunSql(Tool[_SqlArgs]):
    """``run_sql`` that, while ``block`` is set, parks until cancelled."""

    def __init__(self) -> None:
        self.block = False
        self.started = asyncio.Event()

    @property
    def name(self) -> str:
        return "run_sql"

    @property
    def description(self) -> str:
        return "Run SQL"

    def get_args_schema(self) -> type[_SqlArgs]:
        return _SqlArgs

    async def execute(self, context: ToolContext, args: _SqlArgs) -> ToolResult:
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return ToolResult(success=True, result_for_llm="1 row")


def _agent(tool: Tool[Any]) -> tuple[Agent, _Messages, BoundedConversationStore]:
    service = AnthropicLlmService(api_key="test-key")
    messages = _Messages()
    service._client = SimpleNamespace(messages=messages)
    registry = ToolRegistry()
    registry.register_local_tool(tool, access_groups=[DEFAULT_USER_GROUP])
    ui_features = UiFeatures()
    ui_features.register_feature(
        UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS,
        [
            *ui_features.feature_group_access.get(UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS, []),
            DEFAULT_USER_GROUP,
        ],
    )
    store = BoundedConversationStore()
    agent = Agent(
        llm_service=service,
        tool_registry=registry,
        user_resolver=_StaticUserResolver(),
        agent_memory=StubAgentMemory(),
        conversation_store=store,
        config=AgentConfig(
            max_tool_iterations=5,
            max_tokens=4096,
            ui_features=ui_features,
            stream_responses=False,
        ),
    )
    return agent, messages, store


@pytest.fixture
def cfg(tmp_path: Path):
    return build_test_config(persist_dir=tmp_path / "chroma")


async def _call(cfg: Any, agent: Agent, question: str):
    return await query_database_impl_with_widgets(
        cfg, question, conversation_id=_CONVERSATION, agent=agent
    )


async def _history(store: BoundedConversationStore) -> list[tuple[str, str]]:
    user = await _StaticUserResolver().resolve_user(None)
    conversation = await store.get_conversation(_CONVERSATION, user)
    assert conversation is not None
    return [(m.role, m.content) for m in conversation.messages]


def _assert_every_tool_use_answered(payload: dict[str, Any]) -> None:
    messages = payload["messages"]
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or isinstance(message["content"], str):
            continue
        ids = {b["id"] for b in message["content"] if b.get("type") == "tool_use"}
        if not ids:
            continue
        assert index + 1 < len(messages), f"tool_use {ids} is the last message"
        following = messages[index + 1]
        answered = {
            b["tool_use_id"]
            for b in following["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        assert ids <= answered, f"tool_use {ids - answered} has no tool_result after it"


async def _seed(cfg: Any, agent: Agent, store: BoundedConversationStore) -> list[tuple[str, str]]:
    """One completed tool round on C, so the call under test has history before it."""
    markdown, *_ = await _call(cfg, agent, f"first, {_TOOL_PROMPT}")
    assert markdown.startswith("Done.")
    history = await _history(store)
    assert [role for role, _ in history] == ["user", "assistant", "tool", "assistant"]
    return history


async def _assert_next_call_succeeds(
    cfg: Any, agent: Agent, messages: _Messages
) -> None:
    before = len(messages.requests)
    markdown, *_ = await _call(cfg, agent, "and the next question")
    assert markdown.startswith("Done.")
    assert len(messages.requests) == before + 1
    _assert_every_tool_use_answered(messages.requests[-1])


async def test_cancel_between_tool_use_and_tool_result_restores_history(cfg) -> None:
    tool = _GatedRunSql()
    agent, messages, store = _agent(tool)
    before = await _seed(cfg, agent, store)

    tool.block = True
    task = asyncio.create_task(_call(cfg, agent, f"second, {_TOOL_PROMPT}"))
    await tool.started.wait()
    tool.started.clear()
    # The tool is running, so the assistant tool_use is saved and its result is not.
    assert (await _history(store))[-1][0] == "assistant"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _history(store) == before

    tool.block = False
    await _assert_next_call_succeeds(cfg, agent, messages)


def _failing_execute(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("tool registry exploded")


async def test_error_answer_with_unanswered_tool_use_is_trimmed(
    cfg, caplog: pytest.LogCaptureFixture
) -> None:
    tool = _GatedRunSql()
    agent, messages, store = _agent(tool)
    before = await _seed(cfg, agent, store)

    real_execute = agent.tool_registry.execute
    agent.tool_registry.execute = _failing_execute  # type: ignore[method-assign]
    question = f"second, {_TOOL_PROMPT}"
    with caplog.at_level("WARNING", logger="sqllens.tools.query_database"):
        with pytest.raises(AgentRunError) as excinfo:
            await _call(cfg, agent, question)
    assert str(excinfo.value).startswith("SQL execution error: ")
    # Cut back to just before the unanswered assistant message: the call's user
    # message stays.
    assert await _history(store) == [*before, ("user", question)]
    assert any(
        r.levelname == "WARNING"
        and _CONVERSATION in r.getMessage()
        and "removed 1 message(s)" in r.getMessage()
        for r in caplog.records
    )

    agent.tool_registry.execute = real_execute  # type: ignore[method-assign]
    await _assert_next_call_succeeds(cfg, agent, messages)


async def test_raising_send_message_with_unanswered_tool_use_is_trimmed(cfg) -> None:
    tool = _GatedRunSql()
    agent, messages, store = _agent(tool)
    before = await _seed(cfg, agent, store)

    real_send = agent.send_message

    async def raising_send(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for component in real_send(*args, **kwargs):
            yield component
        raise RuntimeError("driver exploded")

    real_execute = agent.tool_registry.execute
    agent.tool_registry.execute = _failing_execute  # type: ignore[method-assign]
    agent.send_message = raising_send  # type: ignore[method-assign]
    question = f"second, {_TOOL_PROMPT}"
    with pytest.raises(RuntimeError) as excinfo:
        await _call(cfg, agent, question)
    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == query_database_module._INTERNAL_ERROR_MESSAGE
    assert await _history(store) == [*before, ("user", question)]

    agent.tool_registry.execute = real_execute  # type: ignore[method-assign]
    agent.send_message = real_send  # type: ignore[method-assign]
    await _assert_next_call_succeeds(cfg, agent, messages)


async def test_answered_tool_rounds_are_kept_as_appended(cfg) -> None:
    tool = _GatedRunSql()
    agent, _messages, store = _agent(tool)
    before = await _seed(cfg, agent, store)
    markdown, *_ = await _call(cfg, agent, f"second, {_TOOL_PROMPT}")
    assert markdown.startswith("Done.")
    history = await _history(store)
    assert history[: len(before)] == before
    assert [role for role, _ in history[len(before):]] == [
        "user", "assistant", "tool", "assistant"
    ]


class _NoTruncateStore(BoundedConversationStore):
    truncate_messages = None  # type: ignore[assignment]


class _RaisingTruncateStore(BoundedConversationStore):
    def truncate_messages(self, conversation_id: str, user_id: str, length: int) -> bool:
        raise RuntimeError("store exploded")


class _OwnerMismatchTruncateStore(BoundedConversationStore):
    def truncate_messages(self, conversation_id: str, user_id: str, length: int) -> bool:
        raise ConversationOwnerMismatchError(conversation_id)


class _UnscannableReadStore(BoundedConversationStore):
    """Once armed, the trim's read returns a message the scan cannot read."""

    armed = False
    reads = 0

    async def get_conversation(self, conversation_id: str, user: Any) -> Any:
        conversation = await super().get_conversation(conversation_id, user)
        if self.armed:
            self.reads += 1
            if self.reads >= 3 and conversation is not None:
                return SimpleNamespace(messages=[*conversation.messages, object()])
        return conversation


class _NoopTruncateStore(BoundedConversationStore):
    def truncate_messages(self, conversation_id: str, user_id: str, length: int) -> bool:
        return False


class _RaisingReadStore(BoundedConversationStore):
    """Fails the read only once armed, so the agent itself still works."""

    armed = False
    reads = 0

    async def get_conversation(self, conversation_id: str, user: Any) -> Any:
        if self.armed:
            self.reads += 1
            # The baseline read and the agent's own load come first; the
            # third read of the call is the trim's.
            if self.reads >= 3:
                raise RuntimeError("store exploded")
        return await super().get_conversation(conversation_id, user)


@pytest.mark.parametrize(
    ("store_cls", "breadcrumb"),
    [
        (_NoTruncateStore, "has no truncate_messages"),
        (_RaisingTruncateStore, "raised from truncate_messages"),
        (_NoopTruncateStore, "truncate_messages removed nothing"),
        (_OwnerMismatchTruncateStore, "raised ConversationOwnerMismatchError"),
        (_UnscannableReadStore, "could not be scanned"),
        (_RaisingReadStore, "raised from get_conversation"),
    ],
)
async def test_trim_that_cannot_run_is_logged_and_keeps_the_calls_result(
    cfg, caplog: pytest.LogCaptureFixture, store_cls: type, breadcrumb: str
) -> None:
    tool = _GatedRunSql()
    agent, _messages, _store = _agent(tool)
    store = store_cls()
    agent.conversation_store = store
    await _seed(cfg, agent, store)

    agent.tool_registry.execute = _failing_execute  # type: ignore[method-assign]
    store.armed = True
    with caplog.at_level("ERROR", logger="sqllens.tools.query_database"):
        with pytest.raises(AgentRunError) as excinfo:
            await _call(cfg, agent, f"second, {_TOOL_PROMPT}")
    store.armed = False
    assert str(excinfo.value).startswith("SQL execution error: ")
    roles = [role for role, _ in await _history(store)]
    # Nothing was removed: the unanswered assistant tool_use is still last.
    assert roles[-2:] == ["user", "assistant"]
    assert any(
        r.levelname == "ERROR"
        and breadcrumb in r.getMessage()
        and store_cls.__name__ in r.getMessage()
        for r in caplog.records
    )
