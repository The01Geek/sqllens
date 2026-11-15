# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The SQL a ``show_details`` answer shows on a server with a rewriting guard (#403).

A rule-set file or an ``[[rls]]`` rule makes SQL Lens rewrite the model's SQL
before MySQL runs it, so the answer labels that SQL as the query as written and
prefixes it with the note line. The stub agent below runs the model's SQL
through the real guard over a recording runner, so the tests can check that
the rewritten text the guard produced never reaches the answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import sqlglot

import sqllens.server as server_module
from sqllens import rule_sets as rule_sets_module
from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import _RuleSetUserResolver
from sqllens.config import AgentRuntimeConfig, Config, RlsRule
from sqllens.conversation_store import BoundedConversationStore
from sqllens.safety import RlsGuardRunner
from sqllens.safety.rule_sets import RuleSetGuardRunner
from sqllens.server import build_server
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import (
    AS_WRITTEN_NOTE_LINE,
    AgentRunError,
    _append_sql_block,
    query_database_impl_with_widgets,
)

from ._agent_stubs import make_dataframe, make_status_card, make_text_component, make_tool_cards
from ._config_builders import build_test_config
from ._rule_set_fixtures import MYSQL_URL, row_rule_document, rule_set_config

NOTE = (
    "-- SQL Lens applies this server's access rules to queries before running them, "
    "so the query that ran can differ from this text."
)
OWNER = {"role": "2", "account": "5", "user": 7}
RULE_SET_SQL = "SELECT COUNT(*) FROM orders o WHERE o.total > 100"
RLS_SQL = "SELECT id, total FROM orders WHERE total > 100"


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    query_database_module._CONVERSATION_QUEUES.clear()
    yield
    query_database_module._CONVERSATION_QUEUES.clear()


@pytest.fixture
def confirmed_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", lambda _url, _loaded: None)


class _Recorder(SqlRunner):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        self.calls.append(args.sql)
        return pd.DataFrame({"n": [77]})


class _GuardedAgent:
    """Runs the model's SQL through ``guard`` and emits what the real agent emits."""

    def __init__(self, guard: SqlRunner, sql: str, *, fail: bool = False) -> None:
        self.conversation_store = BoundedConversationStore()
        self.user_resolver = _RuleSetUserResolver()
        self.agent_memory = None
        self.guard = guard
        self.sql = sql
        self.fail = fail

    def send_message(
        self, request_context: Any, message: str, *, conversation_id: str | None = None
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        await self.guard.run_sql(
            RunSqlToolArgs(sql=self.sql), ToolContext.model_construct(metadata={})
        )
        for card in make_tool_cards("run_sql", {"sql": self.sql}):
            yield card
        yield make_status_card(
            title="Executing run_sql", status="success", description="ran",
            metadata={"sql": self.sql},
        )
        if self.fail:
            yield make_status_card(title="Error", status="error", description="boom")
            return
        yield make_dataframe([{"n": 77}])
        yield make_text_component("77 orders")


def _with_details(cfg: Config, *, show_details: bool = True) -> Config:
    cfg.agent = AgentRuntimeConfig(show_details=show_details)
    return cfg


def _rule_set_case(tmp_path: Path, *, show_details: bool = True, fail: bool = False):
    cfg = _with_details(rule_set_config(tmp_path, row_rule_document()), show_details=show_details)
    recorder = _Recorder()
    agent = _GuardedAgent(
        RuleSetGuardRunner(recorder, cfg.rule_sets.loaded), RULE_SET_SQL, fail=fail
    )
    return cfg, agent, recorder, RULE_SET_SQL, OWNER


def _rls_case(tmp_path: Path, *, show_details: bool = True, fail: bool = False):
    cfg = _with_details(build_test_config(persist_dir=tmp_path / "chroma"),
                        show_details=show_details)
    cfg.database.url = MYSQL_URL
    cfg.rls = [RlsRule(table="orders", column="tenant_id", value="acme")]
    recorder = _Recorder()
    guard = RlsGuardRunner(recorder, cfg.rls, dialect="mysql")
    return cfg, _GuardedAgent(guard, RLS_SQL, fail=fail), recorder, RLS_SQL, {}


CASES = [pytest.param(_rule_set_case, id="rule-set"), pytest.param(_rls_case, id="rls")]


async def _impl(cfg, agent, meta):
    return await query_database_impl_with_widgets(
        cfg, "how many orders?", metadata=meta, conversation_id="conv", agent=agent
    )


def _patch_server(monkeypatch: pytest.MonkeyPatch, agent, meta) -> None:
    real = query_database_impl_with_widgets

    async def impl(cfg, question, **kwargs):
        kwargs["metadata"] = meta
        return await real(cfg, question, agent=agent, **kwargs)

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", impl)


class _Ctx:
    class _RC:
        meta = None

    request_context = _RC()


async def _call_tool(cfg, monkeypatch, agent, meta):
    _patch_server(monkeypatch, agent, meta)
    fn = build_server(cfg)._tool_manager.get_tool("query_database").fn
    return await fn("how many orders?", _Ctx(), conversation_id="conv")


def test_note_line_is_the_specified_text() -> None:
    assert AS_WRITTEN_NOTE_LINE == NOTE


def test_sql_block_without_a_rewriting_guard_is_unchanged() -> None:
    out = _append_sql_block("answer", {"sql": "SELECT 1", "query_type": "SELECT"})
    assert out == "answer\n\n**Executed SQL:**\n\n```sql\nSELECT 1\n```"


def test_sql_block_with_a_rewriting_guard_is_the_query_as_written() -> None:
    out = _append_sql_block(
        "answer", {"sql": "SELECT 1", "query_type": "SELECT"}, as_written=True
    )
    assert out == f"answer\n\n**Query as written:**\n\n```sql\n{NOTE}\nSELECT 1\n```"


def test_sql_block_fence_is_sized_from_the_emitted_text() -> None:
    sql = "SELECT '```' AS x"
    out = _append_sql_block("a", {"sql": sql}, as_written=True)
    assert out.endswith(f"````sql\n{NOTE}\n{sql}\n````")


def test_note_line_changes_nothing_mysql_runs() -> None:
    emitted = f"{NOTE}\n{RULE_SET_SQL}"
    assert sqlglot.parse_one(emitted, read="mysql").sql(dialect="mysql", comments=False) == (
        sqlglot.parse_one(RULE_SET_SQL, read="mysql").sql(dialect="mysql", comments=False)
    )


@pytest.mark.parametrize("case", CASES)
async def test_markdown_and_query_info_under_a_rewriting_guard(
    tmp_path: Path, confirmed_catalog, case
) -> None:
    cfg, agent, recorder, sql, meta = case(tmp_path)
    markdown, _blocks, query_info, _memory, trace = await _impl(cfg, agent, meta)

    assert markdown.endswith(f"\n\n**Query as written:**\n\n```sql\n{NOTE}\n{sql}\n```")
    assert "Executed SQL" not in markdown
    # verify-memory compares this dict's SQL with golden SQL: no note line.
    assert query_info == {"sql": sql, "query_type": "SELECT", "row_count": 1}
    run_sql_args = [s["arguments"] for s in trace["steps"] if s["tool"] == "run_sql"]
    assert run_sql_args and all(a == {"sql": sql} for a in run_sql_args)
    # The guard rewrote the SQL; that text and its predicate stay out of the answer.
    (ran,) = recorder.calls
    assert ran != sql and ran not in markdown


@pytest.mark.parametrize("case", CASES)
async def test_tool_result_under_a_rewriting_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_catalog, case
) -> None:
    cfg, agent, recorder, sql, meta = case(tmp_path)
    result = await _call_tool(cfg, monkeypatch, agent, meta)

    assert result.meta["sqllens/query"] == {
        "sql": f"{NOTE}\n{sql}",
        "query_type": "SELECT",
        "row_count": 1,
        "as_written": True,
    }
    text = result.content[0].text
    answer, footer = text.split("\n\n_Conversation ID: `conv`")
    assert answer.endswith(f"\n\n**Query as written:**\n\n```sql\n{NOTE}\n{sql}\n```")
    assert "```" not in footer
    (ran,) = recorder.calls
    for surface in (text, result.meta["sqllens/query"]["sql"]):
        assert ran not in surface
    trace_sql = [s["arguments"]["sql"] for s in result.meta["sqllens/agent_trace"]["steps"]]
    assert trace_sql and all(s == sql for s in trace_sql)


async def test_rule_set_predicate_stays_out_of_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_catalog
) -> None:
    cfg, agent, recorder, _sql, meta = _rule_set_case(tmp_path)
    result = await _call_tool(cfg, monkeypatch, agent, meta)
    (ran,) = recorder.calls
    assert "void" in ran and "customer_reps" in ran
    for surface in (result.content[0].text, result.meta["sqllens/query"]["sql"]):
        assert "void" not in surface and "customer_reps" not in surface


async def test_rls_predicate_stays_out_of_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, agent, recorder, _sql, meta = _rls_case(tmp_path)
    result = await _call_tool(cfg, monkeypatch, agent, meta)
    (ran,) = recorder.calls
    assert "tenant_id" in ran and "acme" in ran
    for surface in (result.content[0].text, result.meta["sqllens/query"]["sql"]):
        assert "tenant_id" not in surface and "acme" not in surface


async def test_no_rewriting_guard_keeps_the_executed_sql_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _with_details(build_test_config(persist_dir=tmp_path / "chroma"))
    agent = _GuardedAgent(_Recorder(), RLS_SQL)
    result = await _call_tool(cfg, monkeypatch, agent, {})
    assert result.meta["sqllens/query"] == {
        "sql": RLS_SQL, "query_type": "SELECT", "row_count": 1,
    }
    assert f"**Executed SQL:**\n\n```sql\n{RLS_SQL}\n```" in result.content[0].text
    assert NOTE not in result.content[0].text


@pytest.mark.parametrize("case", CASES)
async def test_details_off_under_a_rewriting_guard_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_catalog, case
) -> None:
    cfg, agent, _recorder, _sql, meta = case(tmp_path, show_details=False)
    result = await _call_tool(cfg, monkeypatch, agent, meta)
    text = result.content[0].text
    assert "```sql" not in text and NOTE not in text
    assert "sqllens/query" not in result.meta


@pytest.mark.parametrize("case", CASES)
async def test_failed_turn_under_a_rewriting_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_catalog, case
) -> None:
    cfg, agent, _recorder, sql, meta = case(tmp_path, fail=True)
    with pytest.raises(AgentRunError) as info:
        await _impl(cfg, agent, meta)
    assert info.value.query_info == {"sql": sql, "query_type": "SELECT"}

    cfg.agent = AgentRuntimeConfig(show_details=True, show_conversation_footer=False)
    agent2 = _GuardedAgent(agent.guard, sql, fail=True)
    result = await _call_tool(cfg, monkeypatch, agent2, meta)
    assert result.is_error
    assert "sqllens/query" not in (result.meta or {})
