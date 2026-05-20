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

import pytest
from pydantic import SecretStr

from sqllens.agent.factory import build_agent
from sqllens.agent.tools import (
    EmitChartTool,
    RunSqlTool,
    SaveTextMemoryTool,
    SearchSavedCorrectToolUsesTool,
)
from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
)

from ._config_builders import build_test_config


def _unwrap(tool: object) -> object:
    """Strip the ToolRegistry's access-group wrapper if present."""
    return getattr(tool, "_wrapped_tool", tool)


def test_run_sql_scratch_anchored_to_absolute_tempdir(tmp_path: Path) -> None:
    """build_agent must inject an absolute, user-writable scratch root.

    Regression guard for issue #10: a future re-lift or refactor that drops
    the ``file_system=`` kwarg silently re-introduces the Windows-only
    Claude Desktop CWD failure.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
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
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = build_agent(cfg)
    assert agent.config.max_tool_iterations == 20


def test_max_tool_iterations_flows_through_config(tmp_path: Path) -> None:
    """A config override must reach the underlying agent unchanged."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(max_tool_iterations=42),
    )
    agent = build_agent(cfg)
    assert agent.config.max_tool_iterations == 42


def test_bounded_conversation_store_is_wired(tmp_path: Path) -> None:
    """build_agent wires the bounded LRU store (not the framework's unbounded
    default) with the configured cap, so multi-turn conversations persist but a
    long-running server cannot leak conversations."""
    from sqllens.conversation_store import BoundedConversationStore

    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(max_conversations=5),
    )
    agent = build_agent(cfg)
    assert isinstance(agent.conversation_store, BoundedConversationStore)
    assert agent.conversation_store._max == 5


def test_emit_chart_tool_is_registered(tmp_path: Path) -> None:
    """Without ``emit_chart`` in the ToolRegistry the LLM never sees it and
    ``visualize_data`` silently degrades to text-only — exactly the silent
    failure the precedent ``test_save_text_memory_tool_is_registered`` exists
    to catch. Pins that a future re-lift or factory refactor cannot drop the
    registration without test signal.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = build_agent(cfg)

    assert "emit_chart" in agent.tool_registry._tools
    emit_chart_tool = _unwrap(agent.tool_registry._tools["emit_chart"])
    assert isinstance(emit_chart_tool, EmitChartTool)


def test_show_details_on_unlocks_only_tool_arguments(tmp_path: Path) -> None:
    """show_details (default True) admits the static group to tool_arguments
    *only* — every other admin-gated UI feature stays admin-only, and the
    module-level DEFAULT_UI_FEATURES list is not mutated.
    """
    from sqllens.agent.core.agent.config import (
        DEFAULT_UI_FEATURES,
        UiFeature,
    )

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    assert cfg.agent.show_details is True
    agent = build_agent(cfg)

    fga = agent.config.ui_features.feature_group_access
    assert "default" in fga[UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS]
    # Targeted: the other admin-only features are untouched.
    assert fga[UiFeature.UI_FEATURE_SHOW_TOOL_ERROR] == ["admin"]
    assert fga[UiFeature.UI_FEATURE_SHOW_MEMORY_DETAILED_RESULTS] == ["admin"]
    # The shared module-level default must not have been mutated in place.
    assert DEFAULT_UI_FEATURES[UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS] == [
        "admin"
    ]


def test_show_details_off_keeps_tool_arguments_admin_only(tmp_path: Path) -> None:
    """show_details=False → tool_arguments stays admin-gated, so the static
    user never sees the executed-SQL card (pre-feature behavior).

    Also exercises the actual gate function the agent calls
    (can_user_access_feature) with the resolved static user, end-to-end:
    config → factory → AgentConfig.ui_features → gate verdict. This pins
    the chain the _format.py docstring's "show_details off → no SQL card
    is ever emitted" invariant depends on.
    """
    from sqllens.agent import User
    from sqllens.agent.core.agent.config import UiFeature

    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=False),
    )
    agent = build_agent(cfg)

    fga = agent.config.ui_features.feature_group_access
    assert fga[UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS] == ["admin"]

    static_user = User(id="anyone", group_memberships=["default"])
    assert (
        agent.config.ui_features.can_user_access_feature(
            UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS, static_user
        )
        is False
    )


def test_show_details_on_grants_static_user_access_to_tool_arguments(
    tmp_path: Path,
) -> None:
    """End-to-end gate check for the show_details=True chain: config → factory
    → AgentConfig.ui_features.can_user_access_feature returns True for the
    static DEFAULT_USER_GROUP user, which is what the agent calls before
    yielding the run_sql STATUS_CARD. This is the missing integration link
    between the access-list state (already pinned) and the framework gate
    function (previously trusted by inspection)."""
    from sqllens.agent import User
    from sqllens.agent.core.agent.config import UiFeature

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    assert cfg.agent.show_details is True
    agent = build_agent(cfg)

    static_user = User(id="anyone", group_memberships=["default"])
    assert (
        agent.config.ui_features.can_user_access_feature(
            UiFeature.UI_FEATURE_SHOW_TOOL_ARGUMENTS, static_user
        )
        is True
    )
    # And the other admin features are still locked for the static user.
    assert (
        agent.config.ui_features.can_user_access_feature(
            UiFeature.UI_FEATURE_SHOW_TOOL_ERROR, static_user
        )
        is False
    )


def test_save_text_memory_tool_is_registered(tmp_path: Path) -> None:
    """The default system prompt's text-memory instructions only fire when
    ``save_text_memory`` is registered (``has_text_memory`` in default.py).
    Without this wiring the LLM never sees the tool, so free-form domain
    knowledge can never be persisted — see issue #76.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    agent = build_agent(cfg)

    assert "save_text_memory" in agent.tool_registry._tools
    save_text_memory_tool = _unwrap(agent.tool_registry._tools["save_text_memory"])
    assert isinstance(save_text_memory_tool, SaveTextMemoryTool)


def test_memory_similarity_threshold_flows_into_search_tool(tmp_path: Path) -> None:
    """``cfg.memory.similarity_threshold`` must become the effective default
    used by ``search_saved_correct_tool_uses`` when the LLM omits the per-call
    argument — otherwise the operator-facing config knob is dead (issue #76).
    """
    cfg = Config(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma", similarity_threshold=0.42),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )
    agent = build_agent(cfg)

    search_tool = _unwrap(agent.tool_registry._tools["search_saved_correct_tool_uses"])
    assert isinstance(search_tool, SearchSavedCorrectToolUsesTool)
    assert search_tool._default_similarity_threshold == 0.42


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


@pytest.mark.parametrize("read_only", [True, False])
def test_readonly_guard_wraps_iff_read_only_enabled(
    tmp_path: Path, read_only: bool
) -> None:
    """``ReadOnlyGuardRunner`` wraps the stack iff ``database.read_only=True``.

    A refactor that flips the default or drops the conditional wrap silently
    disables the parser guard — this pins the wiring both ways. When enabled
    the guard must be the OUTERMOST decorator (parse-time reject before any
    row-cap / engine work).
    """
    from sqllens.agent.integrations.sqlite import SqliteRunner
    from sqllens.safety import ReadOnlyGuardRunner

    cfg = Config(
        database=DatabaseConfig(url="sqlite:///:memory:", read_only=read_only),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )
    agent = build_agent(cfg)
    runner = _unwrap(agent.tool_registry._tools["run_sql"]).sql_runner

    if read_only:
        assert isinstance(runner, ReadOnlyGuardRunner), (
            "read_only=True must wrap the runner in ReadOnlyGuardRunner"
        )
    else:
        assert not isinstance(runner, ReadOnlyGuardRunner), (
            "read_only=False must NOT wrap in ReadOnlyGuardRunner"
        )

        def _walk(r: object) -> bool:
            while r is not None:
                if isinstance(r, ReadOnlyGuardRunner):
                    return True
                r = getattr(r, "_inner", None)
            return False

        assert not _walk(runner), "no ReadOnlyGuardRunner anywhere in the stack"

    # The connector-level read-only flag must track the same config flag.
    leaf = runner
    while getattr(leaf, "_inner", None) is not None:
        leaf = leaf._inner
    assert isinstance(leaf, SqliteRunner)
    assert leaf._read_only is read_only


def test_build_sql_runner_mysql_percent_decodes_credentials() -> None:
    """A '/' in a MySQL password must be written %2F in the URL (a raw '/'
    breaks urlparse host detection) and must reach the MySQLRunner decoded.
    Without the unquote, the literal '%2F' is sent as the password and auth
    fails with a misleading "Access denied (using password: YES)".
    """
    pytest.importorskip("pymysql")
    from sqllens.agent.factory import build_sql_runner
    from sqllens.agent.integrations.mysql import MySQLRunner

    runner = build_sql_runner(
        "mysql+pymysql://user%40corp:p%2Fw%3As@db.internal:3306/shop"
    )

    assert isinstance(runner, MySQLRunner)
    assert runner.user == "user@corp"
    assert runner.password == "p/w:s"
