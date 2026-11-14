# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Refusal log lines and the per-request audit line of ``query_database`` under rule sets.

Each test captures the log records of one ``query_database`` call and checks
the WARNING refusal line (class and kept ``_meta`` key names, never values)
and the single INFO ``rule-set audit:`` line, on every ending.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from sqllens import rule_sets as rule_sets_module
from sqllens import server as server_module
from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.rule_sets import RuleSetCatalogError, RuleSetRequestError
from sqllens.safety.rule_sets import RefusalRedactingRunner, RuleSetGuardRunner
from sqllens.tools.query_database import AgentRunError

from ._agent_stubs import make_agent_error_card
from ._config_builders import build_test_config
from ._rule_set_fixtures import rule_set_config
from .test_query_database_rule_sets import (  # noqa: F401  (fixtures)
    OWNER,
    _Agent,
    _ask,
    _reset_conversation_queues,
    _until,
    confirmed_catalog,
)

pytestmark = pytest.mark.usefixtures("confirmed_catalog")

# Values that must never reach a log line.
SECRET_VALUE = "s3cret-value"
EXTRA = {"tenant_note": SECRET_VALUE}


def _audit(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    lines = [r for r in records if r.getMessage().startswith("rule-set audit: ")]
    assert all(r.levelno == logging.INFO for r in lines)
    return [json.loads(r.getMessage().removeprefix("rule-set audit: ")) for r in lines]


def _refusals(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "refused" in r.getMessage() and r.levelno == logging.WARNING]


async def _call(cfg, agent, meta, caplog, **kwargs) -> list[logging.LogRecord]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        try:
            await _ask(cfg, agent, meta, **kwargs)
        except (RuleSetRequestError, AgentRunError, RuntimeError):
            pass
    records = list(caplog.records)
    assert all(SECRET_VALUE not in r.getMessage() for r in records)
    return records


async def test_served_request_writes_one_audit_line_naming_all_three(
    tmp_path: Path, caplog
) -> None:
    records = await _call(rule_set_config(tmp_path), _Agent(), OWNER | EXTRA, caplog)
    assert _audit(records) == [
        {"outcome": "served", "rule_set": "2", "account_id": "5", "user_id": 7}
    ]
    assert _refusals(records) == []


@pytest.mark.parametrize(
    ("meta", "refusal_class", "identity"),
    [
        ({"account": "5", "user": 7}, "rule-set key missing", {}),
        ({"role": "9", "account": "5", "user": 7}, "no such rule set", {}),
        ({"role": "2", "account": "8", "user": 7}, "account not bound", {"rule_set": "2"}),
        (
            {"role": "2", "account": "5", "user": "x"},
            "user ID invalid",
            {"rule_set": "2", "account_id": "5"},
        ),
        (
            {"role": "3", "account": "5", "user": 7},
            "rule set not confirmed",
            {"account_id": "5", "user_id": 7},
        ),
    ],
)
async def test_each_request_class_logs_one_refusal_and_one_audit_line(
    tmp_path: Path, caplog, meta, refusal_class, identity
) -> None:
    records = await _call(rule_set_config(tmp_path), _Agent(), meta | EXTRA, caplog)
    (refusal,) = _refusals(records)
    keys = json.dumps(sorted(meta | EXTRA))
    assert refusal.getMessage() == (
        f'rule-set request refused: class="{refusal_class}"; kept _meta keys={keys}'
    )
    assert _audit(records) == [{"outcome": f"refused: {refusal_class}", **identity}]


async def test_owner_and_existence_refusals_log_class_keys_and_full_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    await _ask(cfg, agent, OWNER)
    intruder = {"role": "2", "account": "5", "user": 8}
    records = await _call(cfg, agent, intruder, caplog)
    (refusal,) = _refusals(records)
    assert refusal.getMessage().startswith(
        'rule-set request refused: class="this conversation was started under another'
    )
    assert _audit(records) == [{
        "outcome": "refused: this conversation was started under another rule set, "
        "account or user",
        "rule_set": "2", "account_id": "5", "user_id": 8,
    }]

    def mismatch(url: str, loaded: Any) -> None:
        raise RuleSetCatalogError("column customers.name does not exist")

    (tmp_path / "other").mkdir()
    cfg = rule_set_config(tmp_path / "other")
    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", mismatch)
    records = await _call(cfg, _Agent(), OWNER, caplog)
    refusals = [r for r in _refusals(records) if r.getMessage().startswith("rule-set request")]
    assert len(refusals) == 1
    assert '"the rule-set file could not be confirmed against the database"' in (
        refusals[0].getMessage()
    )
    assert _audit(records)[0]["outcome"].startswith("refused: the rule-set file")


async def test_failed_turn_writes_one_failed_audit_line(
    tmp_path: Path, caplog
) -> None:
    agent = _Agent()

    async def boom(*_: Any, **__: Any):
        raise ValueError("driver error")
        yield  # pragma: no cover

    agent.send_message = boom  # type: ignore[method-assign]
    records = await _call(rule_set_config(tmp_path), agent, OWNER, caplog)
    assert _audit(records) == [
        {"outcome": "failed", "rule_set": "2", "account_id": "5", "user_id": 7}
    ]
    assert _refusals(records) == []


class _Recorder(SqlRunner):
    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        return pd.DataFrame({"ok": [1]})


async def test_turn_ending_on_a_guard_refusal_logs_class_keys_and_fails(
    tmp_path: Path, caplog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    loaded = cfg.rule_sets.loaded
    runner = RefusalRedactingRunner(RuleSetGuardRunner(_Recorder(), loaded))

    async def turn(*_: Any, **__: Any):
        try:
            await runner.run_sql(
                RunSqlToolArgs(sql="SELECT email FROM staff"),
                ToolContext.model_construct(metadata={}),
            )
        except Exception:
            yield make_agent_error_card()

    agent.send_message = turn  # type: ignore[method-assign]
    records = await _call(cfg, agent, OWNER | EXTRA, caplog)
    (refusal,) = _refusals(records)
    keys = json.dumps(sorted(OWNER | EXTRA))
    assert refusal.getMessage().endswith(
        'class="the query reads a table the rule set does not list"; '
        f"kept _meta keys={keys}"
    )
    assert _audit(records) == [
        {"outcome": "failed", "rule_set": "2", "account_id": "5", "user_id": 7}
    ]


async def test_cancelled_call_writes_one_failed_audit_line(
    tmp_path: Path, caplog
) -> None:
    cfg, agent = rule_set_config(tmp_path), _Agent()
    agent.hold = True
    caplog.clear()
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(_ask(cfg, agent, OWNER))
        await asyncio.wait_for(_until(lambda: len(agent.gates) == 1), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _audit(list(caplog.records)) == [
        {"outcome": "failed", "rule_set": "2", "account_id": "5", "user_id": 7}
    ]


async def test_over_limit_meta_logs_the_discard_and_names_no_keys(
    tmp_path: Path, caplog
) -> None:
    """Through the MCP tool: more than 64 keys are discarded and refused as key missing."""
    cfg = rule_set_config(tmp_path)
    meta = {f"key_{i}": SECRET_VALUE for i in range(65)} | OWNER

    class _Ctx:
        class request_context:
            pass

    _Ctx.request_context.meta = meta
    tool = server_module.build_server(cfg)._tool_manager.get_tool("query_database").fn
    caplog.clear()
    with caplog.at_level(logging.INFO):
        with pytest.raises(Exception, match="rule-set key missing"):
            await tool(question="q", ctx=_Ctx())
    records = list(caplog.records)
    (refusal,) = _refusals(records)
    assert refusal.getMessage() == (
        'rule-set request refused: class="rule-set key missing"; '
        "SQL Lens discarded the request _meta for exceeding its limits"
    )
    assert "key_" not in refusal.getMessage() and "role" not in refusal.getMessage()
    assert _audit(records) == [{"outcome": "refused: rule-set key missing"}]


async def test_no_rule_set_file_writes_no_audit_or_refusal_line(
    tmp_path: Path, caplog
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = _Agent()
    agent.user_resolver = SimpleResolver()
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await _ask(cfg, agent, OWNER)
    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "rule-set" in m]


class SimpleResolver:
    async def resolve_user(self, request_context: Any) -> Any:
        from sqllens.agent.core.user import User

        return User(id="u")



async def test_sdk_rejected_arguments_write_no_audit_line(tmp_path: Path, caplog) -> None:
    from mcp import Client

    cfg = rule_set_config(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        async with Client(server_module.build_server(cfg)) as client:
            result = await client.call_tool("query_database", {"question": 7})
    assert result.is_error is True
    assert _audit(list(caplog.records)) == []


_OWN_LOGGERS = ("sqllens.tools.query_database", "sqllens.rule_sets", "sqllens.safety.rule_sets")


def _with_audit(cfg, **audit: Any):
    from sqllens.config import AuditConfig

    agent_cfg = cfg.agent.model_copy(update={"audit": AuditConfig(**audit)})
    return cfg.model_copy(update={"agent": agent_cfg})


async def _records(cfg, agent, meta, caplog) -> list[tuple[str, int, str]]:
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        try:
            await _ask(cfg, agent, meta)
        except RuntimeError:
            pass
    return [(r.name, r.levelno, r.getMessage()) for r in caplog.records if r.name in _OWN_LOGGERS]


async def test_no_rule_set_file_logs_only_the_lines_written_before(tmp_path: Path, caplog) -> None:
    """Without a rule-set file a served call logs nothing here, a failed one only its old line."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = _Agent()
    agent.user_resolver = SimpleResolver()
    assert await _records(cfg, agent, OWNER | EXTRA, caplog) == []

    async def boom(*_: Any, **__: Any):
        raise ValueError("driver error")
        yield  # pragma: no cover

    agent.send_message = boom  # type: ignore[method-assign]
    assert await _records(cfg, agent, OWNER, caplog) == [
        ("sqllens.tools.query_database", logging.ERROR, "agent.send_message failed")
    ]


@pytest.mark.parametrize("with_rule_sets", [True, False])
async def test_agent_audit_settings_change_no_log_line(
    tmp_path: Path, caplog, with_rule_sets: bool
) -> None:
    """``[agent.audit]`` stays unwired: its settings change nothing that is logged."""
    if with_rule_sets:
        base = rule_set_config(tmp_path)
    else:
        base = build_test_config(persist_dir=tmp_path / "chroma")
    seen = []
    for audit in (
        {"enabled": False},
        {"enabled": True, "log_level": "debug", "include_response_text": True},
    ):
        agent = _Agent()
        if not with_rule_sets:
            agent.user_resolver = SimpleResolver()
        seen.append(await _records(_with_audit(base, **audit), agent, OWNER, caplog))
    assert seen[0] == seen[1]
    expected = len(_audit([_Rec(m) for _, _, m in seen[0]]))
    assert expected == (1 if with_rule_sets else 0)


class _Rec(logging.LogRecord):
    def __init__(self, message: str) -> None:
        super().__init__("x", logging.INFO, __file__, 0, message, None, None)
