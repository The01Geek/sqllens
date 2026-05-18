# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for ``factory.build_agent``'s tool wiring.

The bug fixed by issue #10 is silent and platform-conditional — under MCP
launchers with non-writable CWD (Claude Desktop on Windows installs under
``Program Files`` / ``Local\\AnthropicClaude``), ``RunSqlTool`` writes
fail with ``WinError 5``. Linux CI cannot reproduce the runtime failure,
so the only Linux-observable regression surface is the wiring shape in
``factory.py``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import SecretStr

from sqllens.agent.factory import build_agent
from sqllens.agent.tools import RunSqlTool
from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
)


def _build_test_config(
    persist_dir: Path,
    agent: AgentRuntimeConfig | None = None,
) -> Config:
    """Build a Config from kwargs, bypassing env-var resolution.

    Passing every nested model explicitly avoids the default_factory
    re-reading env (which otherwise picks up empty-string overrides
    that fail Literal validation in some test environments).
    """
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=persist_dir),
        auth=AuthConfig(mode="none"),
        agent=agent or AgentRuntimeConfig(),
    )


def _unwrap(tool: object) -> object:
    """Strip the ToolRegistry's access-group wrapper if present."""
    return getattr(tool, "_wrapped_tool", tool)


def test_run_sql_scratch_anchored_to_absolute_tempdir(tmp_path: Path) -> None:
    """build_agent must inject an absolute, user-writable scratch root.

    Regression guard for issue #10: a future re-lift or refactor that drops
    the ``file_system=`` kwarg silently re-introduces the Windows-only
    Claude Desktop CWD failure.
    """
    cfg = _build_test_config(persist_dir=tmp_path / "chroma")
    agent = build_agent(cfg)

    run_sql_tool = _unwrap(agent.tool_registry._tools["run_sql"])
    assert isinstance(run_sql_tool, RunSqlTool)

    working_dir = run_sql_tool.file_system.working_directory
    assert working_dir == Path(tempfile.gettempdir()) / "sqllens"
    assert working_dir.is_absolute(), "scratch root must not depend on process CWD"
    assert working_dir != Path("."), "must not fall through to LocalFileSystem default"


def test_default_max_tool_iterations_is_twenty(tmp_path: Path) -> None:
    """The default surfaces our raised baseline, not the framework's 10.

    Real schema exploration on untrained DBs routinely needs >10 tool calls;
    a regression that drops this default would silently re-introduce the
    "tool iteration limit reached" cutoff users hit on first-time queries.
    """
    cfg = _build_test_config(persist_dir=tmp_path / "chroma")
    agent = build_agent(cfg)
    assert agent.config.max_tool_iterations == 20


def test_max_tool_iterations_flows_through_config(tmp_path: Path) -> None:
    """A config override must reach the underlying agent unchanged."""
    cfg = _build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(max_tool_iterations=42),
    )
    agent = build_agent(cfg)
    assert agent.config.max_tool_iterations == 42


def test_database_timeout_and_cap_flow_through_to_runner(tmp_path: Path) -> None:
    """A regression that drops statement_timeout_ms or max_rows from the runner
    constructor (e.g. a future refactor of build_sql_runner) would silently
    disable the safety primitives — this test pins the wiring shape.

    Decorator stack order is also pinned: ReadOnlyGuardRunner must be outermost
    (parse-time reject), then RowCapRunner (post-execution cap), then the
    engine-specific runner.
    """
    from sqllens.agent.integrations.sqlite import SqliteRunner
    from sqllens.safety import ReadOnlyGuardRunner, RowCapRunner

    cfg = Config(
        database=DatabaseConfig(
            url="sqlite:///:memory:",
            statement_timeout_ms=1234,
            max_rows=77,
        ),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )
    agent = build_agent(cfg)
    run_sql_tool = _unwrap(agent.tool_registry._tools["run_sql"])

    outer = run_sql_tool.sql_runner
    assert isinstance(outer, ReadOnlyGuardRunner)
    cap = outer._inner
    assert isinstance(cap, RowCapRunner)
    assert cap._max_rows == 77
    inner = cap._inner
    assert isinstance(inner, SqliteRunner)
    assert inner._statement_timeout_ms == 1234
    assert inner._max_rows == 77
