# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Build a configured ``Agent`` instance from SQL Lens config.

This module is the boundary between the agent framework (``sqllens.agent.core``,
``sqllens.agent.integrations``) and the rest of SQL Lens. Callers should use
``build_agent(cfg)`` and never reach into the framework directly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqllens.agent import Agent, RequestContext, ToolRegistry, User, UserResolver
from sqllens.agent.capabilities.sql_runner import SqlRunner
from sqllens.agent.core import AgentConfig
from sqllens.agent.integrations import (
    AnthropicLlmService,
    ChromaAgentMemory,
    PostgresRunner,
    SqliteRunner,
)
from sqllens.agent.integrations.local import LocalFileSystem
from sqllens.agent.integrations.mysql import MySQLRunner
from sqllens.agent.tools import (
    RunSqlTool,
    SaveQuestionToolArgsTool,
    SaveTextMemoryTool,
    SearchSavedCorrectToolUsesTool,
)
from sqllens.config import API_KEY_MISSING_MESSAGE, Config
from sqllens.safety import ReadOnlyGuardRunner, RlsGuardRunner, RowCapRunner

DEFAULT_USER_ID = "sqllens-user"
DEFAULT_USER_GROUP = "default"


class _StaticUserResolver(UserResolver):
    """Returns the same single user for every request — single-tenant by design."""

    async def resolve_user(self, request_context: RequestContext) -> User:
        return User(
            id=DEFAULT_USER_ID,
            email=f"{DEFAULT_USER_ID}@local",
            group_memberships=[DEFAULT_USER_GROUP],
        )


def build_agent(cfg: Config) -> Agent:
    """Wire the agent from config. One call per process; the agent is reusable."""
    # Every CLI-launched transport already exits 2 in ``cli.serve`` before reaching
    # here. This guard catches the residual bypass paths — programmatic embedders
    # and tests that call ``build_agent`` directly — so a ``None`` key surfaces as
    # an actionable ``ValueError`` instead of slipping into ``get_secret_value()``
    # and reaching the MCP client as a bare ``AttributeError``, which CLAUDE.md
    # forbids.
    if cfg.llm.api_key is None:
        raise ValueError(API_KEY_MISSING_MESSAGE)
    llm = AnthropicLlmService(
        model=cfg.llm.model,
        api_key=cfg.llm.api_key.get_secret_value(),
    )
    sql_runner = build_sql_runner(
        cfg.database.url,
        statement_timeout_ms=cfg.database.statement_timeout_ms,
        max_rows=cfg.database.max_rows,
        read_only=cfg.database.read_only,
    )
    sql_runner = RowCapRunner(sql_runner, max_rows=cfg.database.max_rows)
    dialect = _sqlglot_dialect(cfg.database.url)
    if cfg.database.read_only:
        sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=dialect)
    # RLS is composed *outermost* (ahead of the read-only guard) so it rewrites
    # the SQL first and the read-only guard then validates the *rewritten*
    # statement — its full-tree walk rejects nested DML/DDL and denied
    # functions on the injected predicates too. Only wrapped when rules exist
    # so the no-RLS path stays a zero-overhead passthrough.
    if cfg.rls:
        sql_runner = RlsGuardRunner(sql_runner, cfg.rls, dialect=dialect)
    memory = ChromaAgentMemory(
        persist_directory=str(cfg.memory.persist_dir),
        collection_name=cfg.memory.collection,
    )

    # Anchor RunSqlTool's scratch CSV writes to an absolute, user-writable temp
    # directory. The default LocalFileSystem() resolves "." against process CWD,
    # which is non-writable under some MCP launchers (e.g. Claude Desktop on
    # Windows installs under Program Files / Local\AnthropicClaude).
    scratch_fs = LocalFileSystem(str(Path(tempfile.gettempdir()) / "sqllens"))

    tools = ToolRegistry()
    access = [DEFAULT_USER_GROUP]
    tools.register_local_tool(
        RunSqlTool(sql_runner=sql_runner, file_system=scratch_fs),
        access_groups=access,
    )
    tools.register_local_tool(SaveQuestionToolArgsTool(), access_groups=access)
    tools.register_local_tool(
        SearchSavedCorrectToolUsesTool(
            default_similarity_threshold=cfg.memory.similarity_threshold,
        ),
        access_groups=access,
    )
    # save_text_memory persists free-form domain knowledge (vocabulary, semantic
    # notes) so subsequent questions can land on the right tables/columns. The
    # default system prompt switches on its presence via has_text_memory.
    tools.register_local_tool(SaveTextMemoryTool(), access_groups=access)

    return Agent(
        llm_service=llm,
        tool_registry=tools,
        user_resolver=_StaticUserResolver(),
        agent_memory=memory,
        # Framework's AgentConfig defaults max_tool_iterations=10, which truncates
        # mid-exploration on untrained schemas. Surface the knob via config so
        # operators can raise it without patching code.
        config=AgentConfig(max_tool_iterations=cfg.agent.max_tool_iterations),
    )


def build_sql_runner(
    url: str,
    *,
    statement_timeout_ms: int = 30_000,
    max_rows: int = 10_000,
    read_only: bool = True,
) -> SqlRunner:
    """Pick the right SQL runner from the database URL prefix.

    ``statement_timeout_ms`` and ``max_rows`` are threaded through so the
    per-engine timeout (SET statement_timeout / MAX_EXECUTION_TIME / progress
    handler) and ``fetchmany(max_rows + 1)`` stream cap run inside the runner.

    ``read_only`` is threaded through so the connector enforces read-only at
    the driver/session layer (SQLite ``mode=ro`` URI, Postgres/MySQL read-only
    transaction) — defence-in-depth so a parser-guard miss still cannot mutate.
    """
    scheme = url.split("://", 1)[0].lower()
    if scheme.startswith("sqlite"):
        # sqlite:///abs/path.db → /abs/path.db ; sqlite://:memory: stays as-is
        path = url.split("://", 1)[1]
        if path.startswith("/"):
            path = path[1:] if not path.startswith("//") else path
        return SqliteRunner(
            database_path=path or ":memory:",
            statement_timeout_ms=statement_timeout_ms,
            max_rows=max_rows,
            read_only=read_only,
        )
    if scheme.startswith("postgres"):
        # SQLAlchemy-style scheme like "postgresql+psycopg2" needs to be normalized
        # for psycopg2 connection strings, which only accept "postgresql://".
        normalized = "postgresql://" + url.split("://", 1)[1]
        return PostgresRunner(
            connection_string=normalized,
            statement_timeout_ms=statement_timeout_ms,
            max_rows=max_rows,
            read_only=read_only,
        )
    if scheme.startswith("mysql"):
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.username:
            raise ValueError("mysql url must include user, host, and database name")
        return MySQLRunner(
            host=parsed.hostname,
            port=parsed.port or 3306,
            database=(parsed.path or "").lstrip("/"),
            # urlparse does not percent-decode credentials (SQLAlchemy's make_url
            # does); decode so e.g. a '%2F' in the password becomes '/'.
            user=unquote(parsed.username),
            password=unquote(parsed.password) if parsed.password else "",
            statement_timeout_ms=statement_timeout_ms,
            max_rows=max_rows,
            read_only=read_only,
        )
    raise ValueError(f"unsupported database scheme: {scheme!r} (expected sqlite/postgres/mysql)")


def _sqlglot_dialect(url: str) -> str | None:
    """Map a database URL to the sqlglot dialect name used by the safety guard."""
    scheme = url.split("://", 1)[0].lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    if scheme.startswith("postgres"):
        return "postgres"
    if scheme.startswith("mysql"):
        return "mysql"
    return None
