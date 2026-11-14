# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``query_database`` under a rule-set file: selection, conversation owner, catalog check.

Drives ``query_database_impl_with_widgets`` with an agent stub that records
the rule set each turn ran under; the stub uses the factory's rule-set user
resolver and the bounded conversation store, so ownership is real.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sqllens import rule_sets as rule_sets_module
from sqllens.agent.core.storage import Message
from sqllens.agent.factory import _RuleSetUserResolver
from sqllens.conversation_store import BoundedConversationStore
from sqllens.rule_sets import (
    REFUSED_CATALOG_UNCONFIRMED,
    REFUSED_CONVERSATION_OWNER,
    REFUSED_RULE_SET_KEY_MISSING,
    REFUSED_RULE_SET_NOT_CONFIRMED,
    RuleSetCatalogError,
    RuleSetRequestError,
    current_rule_set_selection,
)
from sqllens.server import _request_metadata
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import query_database_impl_with_widgets

from ._agent_stubs import make_text_component
from ._config_builders import build_test_config
from ._rule_set_fixtures import rule_set_config

OWNER = {"role": "2", "account": "5", "user": 7}


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    query_database_module._CONVERSATION_QUEUES.clear()
    yield
    query_database_module._CONVERSATION_QUEUES.clear()


class _Agent:
    """Agent stub: one message per turn, held open until its gate is set."""

    def __init__(self) -> None:
        self.conversation_store = BoundedConversationStore()
        self.user_resolver = _RuleSetUserResolver()
        self.agent_memory = None
        self.selections: list[Any] = []
        self.gates: list[asyncio.Event] = []
        self.hold = False

    def send_message(
        self, request_context: Any, message: str, *, conversation_id: str | None = None
    ) -> AsyncIterator[Any]:
        return self._stream(request_context, message, conversation_id)

    async def _stream(
        self, request_context: Any, message: str, conversation_id: str | None
    ) -> AsyncIterator[Any]:
        self.selections.append(current_rule_set_selection())
        user = await self.user_resolver.resolve_user(request_context)
        if conversation_id is not None:
            conversation = await self.conversation_store.get_conversation(conversation_id, user)
            if conversation is None:
                await self.conversation_store.create_conversation(conversation_id, user, message)
            else:
                conversation.add_message(Message(role="user", content=message))
        if self.hold:
            gate = asyncio.Event()
            self.gates.append(gate)
            await gate.wait()
        yield make_text_component(f"answered {message}")


@pytest.fixture
def confirmed_catalog(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def ok(url: str, loaded: Any) -> None:
        calls.append(1)

    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", ok)
    return calls


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.001)


async def _ask(cfg, agent, meta, question="q", conversation_id="conv", profile=None):
    return await query_database_impl_with_widgets(
        cfg, question, metadata=meta, conversation_id=conversation_id, agent=agent,
        profile=profile,
    )


async def test_request_runs_under_its_selection(tmp_path: Path, confirmed_catalog) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    markdown, *_ = await _ask(cfg, agent, OWNER)
    assert "answered q" in markdown
    (selection,) = agent.selections
    assert (selection.name, selection.account_id, selection.user_id) == ("2", "5", 7)
    assert current_rule_set_selection() is None


async def test_request_class_refusal_happens_before_the_agent(
    tmp_path: Path, confirmed_catalog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    for meta, message in (
        ({}, REFUSED_RULE_SET_KEY_MISSING),
        ({"role": "3", "account": "5", "user": 7}, REFUSED_RULE_SET_NOT_CONFIRMED),
    ):
        with pytest.raises(RuleSetRequestError) as info:
            await _ask(cfg, agent, meta)
        assert str(info.value) == message
    assert agent.selections == [] and confirmed_catalog == []


async def test_request_class_refusal_comes_before_profile_lookup(tmp_path: Path) -> None:
    """An invalid profile name would fail too; the request class is reported first."""
    cfg = rule_set_config(tmp_path)
    with pytest.raises(RuleSetRequestError, match="rule-set key missing"):
        await _ask(cfg, _Agent(), {}, profile="not a valid name!")


def test_over_limit_meta_is_refused_as_key_missing(tmp_path: Path) -> None:
    from sqllens.rule_sets import select_rule_set

    # Exactly 64 forwarded keys, so only counting ``progressToken`` beside
    # them puts this request over the limit.
    extras = {f"k{i}": i for i in range(61)} | OWNER
    assert len(extras) == 64
    sent_meta = extras | {"progressToken": "p"}
    context = SimpleNamespace(request_context=SimpleNamespace(meta=sent_meta))
    loaded = rule_set_config(tmp_path).rule_sets.loaded
    assert loaded is not None

    meta = _request_metadata(context, count_declared_keys=True)
    with pytest.raises(RuleSetRequestError) as info:
        select_rule_set(loaded, meta)
    assert str(info.value) == REFUSED_RULE_SET_KEY_MISSING

    # The same request is forwarded whole when the caller does not ask for
    # every key to be counted, so the flag is what refuses it.
    assert _request_metadata(context) == extras


@pytest.mark.parametrize(
    "other",
    [
        {"role": "4", "account": "5", "user": 7},
        {"role": "2", "account": "6", "user": 7},
        {"role": "2", "account": "5", "user": 8},
    ],
)
async def test_conversation_belongs_to_the_identity_that_started_it(
    tmp_path: Path, confirmed_catalog, other: dict[str, Any]
) -> None:
    from ._rule_set_fixtures import example_document

    document = example_document()
    document["rule_sets"].append(
        {**document["rule_sets"][0], "name": "4"}
    )
    cfg, agent = rule_set_config(tmp_path, document), _Agent()
    await _ask(cfg, agent, OWNER, question="first")
    with pytest.raises(RuleSetRequestError) as info:
        await _ask(cfg, agent, other, question="intruder")
    assert str(info.value) == REFUSED_CONVERSATION_OWNER
    await _ask(cfg, agent, OWNER, question="second")
    stored = agent.conversation_store._conversations["conv"]
    assert [m.content for m in stored.messages] == ["first", "second"]
    assert len(agent.selections) == 2


async def test_other_identity_waiting_on_the_owners_first_turn_is_refused(
    tmp_path: Path, confirmed_catalog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    agent.hold = True
    owner_turn = asyncio.create_task(_ask(cfg, agent, OWNER, question="first"))
    # The owner's turn is inside the agent, holding the conversation's turn lock.
    await asyncio.wait_for(_until(lambda: len(agent.gates) == 1), timeout=5)
    intruder = asyncio.create_task(
        _ask(cfg, agent, {"role": "2", "account": "5", "user": 8}, question="intruder")
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert not intruder.done()
    agent.gates[0].set()
    await owner_turn
    with pytest.raises(RuleSetRequestError, match="another rule set, account or user"):
        await intruder
    stored = agent.conversation_store._conversations["conv"]
    assert [m.content for m in stored.messages] == ["first"]


async def test_catalog_check_refuses_until_it_passes_and_logs_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    outcomes = [RuleSetCatalogError("column customers.name does not exist"), None]

    def check(url: str, loaded: Any) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", check)
    with caplog.at_level(logging.WARNING, logger="sqllens.rule_sets"):
        with pytest.raises(RuleSetRequestError) as info:
            await _ask(cfg, agent, OWNER)
    assert str(info.value) == REFUSED_CATALOG_UNCONFIRMED
    assert "customers" not in str(info.value)
    assert any("customers.name" in r.getMessage() for r in caplog.records)
    assert agent.selections == []
    await _ask(cfg, agent, OWNER)
    assert len(agent.selections) == 1
    # Passed once: no further check runs.
    monkeypatch.setattr(
        rule_sets_module, "_check_catalog_blocking",
        lambda *_: pytest.fail("checked again after passing"),
    )
    await _ask(cfg, agent, OWNER, conversation_id="conv-2")


async def test_catalog_check_error_refuses_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()

    def broken(url: str, loaded: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", broken)
    for _ in range(2):
        with pytest.raises(RuleSetRequestError, match="could not be confirmed"):
            await _ask(cfg, agent, OWNER)
    assert cfg.rule_sets.loaded is not None and cfg.rule_sets.loaded.catalog.confirmed is False


def test_request_time_catalog_check_bounds_every_socket_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The check runs under the catalog lock: an unbounded read would hold
    # every later rule-set request instead of refusing it.
    import pymysql

    seen: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise pymysql.err.OperationalError(2013, "Lost connection during query")

    monkeypatch.setattr(pymysql, "connect", fake_connect)
    with pytest.raises(pymysql.err.OperationalError):
        rule_sets_module._check_catalog_blocking("mysql+pymysql://u:p@h:3306/db", None)
    assert seen["connect_timeout"] > 0
    assert seen["read_timeout"] > 0
    assert seen["write_timeout"] > 0


async def test_class_refusal_wins_over_catalog_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = rule_set_config(tmp_path)
    monkeypatch.setattr(
        rule_sets_module, "_check_catalog_blocking",
        lambda *_: pytest.fail("catalog checked for a refused request"),
    )
    with pytest.raises(RuleSetRequestError, match="user ID invalid"):
        await _ask(cfg, _Agent(), {"role": "2", "account": "5", "user": 0})


async def test_profile_does_not_change_the_selection(tmp_path: Path, confirmed_catalog) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    await _ask(cfg, agent, OWNER, profile="details")
    assert agent.selections[0].name == "2"


async def test_without_rule_sets_nothing_is_selected_or_refused(tmp_path: Path) -> None:
    from sqllens.agent.factory import _StaticUserResolver

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = _Agent()
    agent.user_resolver = _StaticUserResolver()
    markdown, *_ = await _ask(cfg, agent, {})
    assert "answered q" in markdown
    assert agent.selections == [None]


def test_query_database_counts_declared_meta_keys_only_under_rule_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool passes the declared-key count exactly when a rule-set file is loaded."""
    import sqllens.server as server_module

    seen: list[bool] = []

    def record(ctx, *, count_declared_keys: bool = False) -> dict[str, object]:
        seen.append(count_declared_keys)
        raise RuntimeError("stop before the agent runs")

    monkeypatch.setattr(server_module, "_request_metadata", record)

    for cfg in (
        rule_set_config(tmp_path),
        build_test_config(persist_dir=tmp_path / "chroma"),
    ):
        tool = server_module.build_server(cfg)._tool_manager.get_tool("query_database").fn
        # The tool wrapper re-raises the error as the SDK's ``ToolError``.
        with pytest.raises(Exception, match="stop before the agent runs"):
            asyncio.run(tool(question="q", ctx=SimpleNamespace()))
    assert seen == [True, False]
