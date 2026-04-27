"""Build a configured ``Agent`` instance from SQL Lens config.

This module is the boundary between the agent framework (``sqllens.agent.core``,
``sqllens.agent.integrations``) and the rest of SQL Lens. Callers should use
``build_agent(cfg)`` and never reach into the framework directly.
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqllens.agent import Agent, RequestContext, ToolRegistry, User, UserResolver
from sqllens.agent.capabilities.sql_runner import SqlRunner
from sqllens.agent.integrations import (
    AnthropicLlmService,
    ChromaAgentMemory,
    PostgresRunner,
    SqliteRunner,
)
from sqllens.agent.integrations.mysql import MySQLRunner
from sqllens.agent.tools import (
    RunSqlTool,
    SaveQuestionToolArgsTool,
    SearchSavedCorrectToolUsesTool,
)
from sqllens.config import Config
from sqllens.safety import ReadOnlyGuardRunner

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
    llm = AnthropicLlmService(
        model=cfg.llm.model,
        api_key=cfg.llm.api_key.get_secret_value(),
    )
    sql_runner = build_sql_runner(cfg.database.url)
    if cfg.database.read_only:
        sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=_sqlglot_dialect(cfg.database.url))
    memory = ChromaAgentMemory(
        persist_directory=str(cfg.memory.persist_dir),
        collection_name=cfg.memory.collection,
    )

    tools = ToolRegistry()
    access = [DEFAULT_USER_GROUP]
    tools.register_local_tool(RunSqlTool(sql_runner=sql_runner), access_groups=access)
    tools.register_local_tool(SaveQuestionToolArgsTool(), access_groups=access)
    tools.register_local_tool(SearchSavedCorrectToolUsesTool(), access_groups=access)

    return Agent(
        llm_service=llm,
        tool_registry=tools,
        user_resolver=_StaticUserResolver(),
        agent_memory=memory,
    )


def build_sql_runner(url: str) -> SqlRunner:
    """Pick the right SQL runner from the database URL prefix."""
    scheme = url.split("://", 1)[0].lower()
    if scheme.startswith("sqlite"):
        # sqlite:///abs/path.db → /abs/path.db ; sqlite://:memory: stays as-is
        path = url.split("://", 1)[1]
        if path.startswith("/"):
            path = path[1:] if not path.startswith("//") else path
        return SqliteRunner(database_path=path or ":memory:")
    if scheme.startswith("postgres"):
        # SQLAlchemy-style scheme like "postgresql+psycopg2" needs to be normalized
        # for psycopg2 connection strings, which only accept "postgresql://".
        normalized = "postgresql://" + url.split("://", 1)[1]
        return PostgresRunner(connection_string=normalized)
    if scheme.startswith("mysql"):
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.username:
            raise ValueError("mysql url must include user, host, and database name")
        return MySQLRunner(
            host=parsed.hostname,
            port=parsed.port or 3306,
            database=(parsed.path or "").lstrip("/"),
            user=parsed.username,
            password=parsed.password or "",
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
