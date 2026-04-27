# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the Postgres and MySQL SqlRunner adapters.

These tests run against **real** Postgres and MySQL instances. They're gated
behind the ``connectors`` pytest marker and are CI-only — locally, run them
explicitly with ``pytest -m connectors``.

Connection URLs come from env vars so the same tests work against
docker-compose, GH Actions service containers, or a developer's local DBs.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.factory import build_sql_runner
from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ServerConfig,
)
from sqllens.safety import ReadOnlyGuardRunner, UnsafeSqlError

pytestmark = [pytest.mark.connectors, pytest.mark.asyncio]


def _need(env_var: str) -> str:
    val = os.environ.get(env_var)
    if not val:
        pytest.skip(f"{env_var} not set; skipping connector test")
    return val


@pytest.fixture
def postgres_url() -> str:
    return _need("SQLLENS_TEST_POSTGRES_URL")


@pytest.fixture
def mysql_url() -> str:
    return _need("SQLLENS_TEST_MYSQL_URL")


def _ctx() -> Any:
    """Minimal stand-in for a ToolContext — the runners only touch a few fields."""

    class _Ctx:
        request_context = None
        user = None
        conversation = None
        output_directory = "/tmp"

    return _Ctx()


# ─────────────────────────────── postgres ───────────────────────────────────


class TestPostgresRunner:
    async def test_select_one(self, postgres_url: str) -> None:
        runner = build_sql_runner(postgres_url)
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS n"), _ctx())
        assert df.shape == (1, 1)
        assert int(df.iloc[0]["n"]) == 1

    async def test_writes_blocked_by_guard(self, postgres_url: str) -> None:
        runner = ReadOnlyGuardRunner(build_sql_runner(postgres_url), dialect="postgres")
        with pytest.raises(UnsafeSqlError):
            await runner.run_sql(
                RunSqlToolArgs(sql=f"CREATE TABLE t_{uuid.uuid4().hex} (a INT)"),
                _ctx(),
            )


# ──────────────────────────────── mysql ─────────────────────────────────────


class TestMysqlRunner:
    async def test_select_one(self, mysql_url: str) -> None:
        runner = build_sql_runner(mysql_url)
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS n"), _ctx())
        assert df.shape == (1, 1)
        assert int(df.iloc[0]["n"]) == 1

    async def test_writes_blocked_by_guard(self, mysql_url: str) -> None:
        runner = ReadOnlyGuardRunner(build_sql_runner(mysql_url), dialect="mysql")
        with pytest.raises(UnsafeSqlError):
            await runner.run_sql(
                RunSqlToolArgs(sql=f"CREATE TABLE t_{uuid.uuid4().hex} (a INT)"),
                _ctx(),
            )


# ──────────────────────────────── factory ───────────────────────────────────


class TestFactoryAcceptsBoth:
    """Smoke check that both schemes route through ``build_sql_runner``."""

    def test_postgres_url_routes_to_postgres_runner(self, postgres_url: str) -> None:
        runner = build_sql_runner(postgres_url)
        assert type(runner).__name__ == "PostgresRunner"

    def test_mysql_url_routes_to_mysql_runner(self, mysql_url: str) -> None:
        runner = build_sql_runner(mysql_url)
        assert type(runner).__name__ == "MySQLRunner"


# ──────────────────────────────── config ────────────────────────────────────


class TestConfigConstruction:
    """Validate that the connector URL fits the Config schema."""

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@h:5432/db",
            "postgresql+psycopg2://u:p@h:5432/db",
            "mysql+pymysql://u:p@h:3306/db",
        ],
    )
    def test_db_url_accepted(self, url: str) -> None:
        cfg = Config.model_construct(
            database=DatabaseConfig(url=url),
            llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
            memory=MemoryConfig(),
            auth=AuthConfig(),
            server=ServerConfig(),
        )
        assert cfg.database.url == url
