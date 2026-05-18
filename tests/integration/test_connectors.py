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
import time
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
from sqllens.safety.limits import MAX_ROWS_ATTR, TRUNCATED_ATTR

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

    async def test_max_rows_cap_truncates(self, postgres_url: str) -> None:
        """generate_series of max_rows + 1 must return exactly max_rows with a truncation marker."""
        max_rows = 50
        runner = build_sql_runner(postgres_url, max_rows=max_rows)
        df = await runner.run_sql(
            RunSqlToolArgs(sql=f"SELECT generate_series(1, {max_rows + 1}) AS n"),
            _ctx(),
        )
        assert len(df) == max_rows
        assert df.attrs[TRUNCATED_ATTR] is True
        assert df.attrs[MAX_ROWS_ATTR] == max_rows

    async def test_statement_timeout_raises_within_a_second(self, postgres_url: str) -> None:
        """pg_sleep(2) with a 500ms timeout must error in under 1s."""
        import psycopg2

        runner = build_sql_runner(postgres_url, statement_timeout_ms=500)
        start = time.monotonic()
        # Catch QueryCanceled specifically — a bare ``Exception`` would also
        # pass for unrelated failures (connection refused, ImportError on a
        # missing driver) and silently mis-pass when the timeout never fires.
        with pytest.raises(psycopg2.errors.QueryCanceled):
            await runner.run_sql(RunSqlToolArgs(sql="SELECT pg_sleep(2)"), _ctx())
        elapsed = time.monotonic() - start
        assert elapsed < 1.5, f"timeout fired too slowly ({elapsed:.2f}s)"


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

    async def test_max_rows_cap_truncates(self, mysql_url: str) -> None:
        """A UNION-built row generator of max_rows + 1 must return exactly max_rows."""
        max_rows = 5
        # MySQL has no generate_series; build a small union to overshoot the cap.
        sql = " UNION ALL ".join(f"SELECT {i} AS n" for i in range(1, max_rows + 2))
        runner = build_sql_runner(mysql_url, max_rows=max_rows)
        df = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())
        assert len(df) == max_rows
        assert df.attrs[TRUNCATED_ATTR] is True
        assert df.attrs[MAX_ROWS_ATTR] == max_rows

    async def test_huge_select_does_not_drain_after_cap(self, mysql_url: str) -> None:
        """Regression: ``SSDictCursor.close()`` MUST NOT be added to the streaming path.

        PyMySQL's ``SSCursor.close()`` (inherited by ``SSDictCursor``) drains
        every remaining row off the wire to keep the connection in sync.
        Adding ``cursor.close()`` to ``run_sql`` would pass every other current
        test (they all use tiny result sets) while defeating the row cap on
        huge result sets — the runner would still return ``max_rows`` rows,
        but only after pulling millions of rows over the wire.

        Cross-join ``information_schema.columns`` against itself — even on a
        fresh MySQL this produces millions of rows. With ``max_rows=5`` the
        runner must return in under a second: a full drain would take many
        seconds at minimum. Wall-clock time is the only signal that
        distinguishes "streamed + capped" from "drained + capped". A 2-way
        join (not 3-way) is plenty for the signal and avoids piling
        unnecessary server-side iteration work on the CI MySQL.
        """
        max_rows = 5
        sql = (
            "SELECT a.TABLE_NAME FROM information_schema.columns a "
            "CROSS JOIN information_schema.columns b"
        )
        runner = build_sql_runner(mysql_url, max_rows=max_rows)
        start = time.monotonic()
        df = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())
        elapsed = time.monotonic() - start

        assert len(df) == max_rows
        assert df.attrs[TRUNCATED_ATTR] is True
        # Bound is generous (1.5s) for slow CI runners. A full drain would be
        # orders of magnitude over this; tightening further would just produce
        # flakes without sharpening the signal.
        assert elapsed < 1.5, (
            f"cap returned in {elapsed:.2f}s — far too slow; cursor likely "
            "drained the full result set (SSDictCursor.close() was probably added)"
        )

    async def test_statement_timeout_raises_within_a_second(self, mysql_url: str) -> None:
        """A long-running SELECT with a 500ms timeout must error in under ~2s.

        ``MAX_EXECUTION_TIME`` in MySQL 8.0 only interrupts read-only SELECTs
        that the executor checks between row reads; ``SELECT SLEEP(2)`` is *not*
        reliably interrupted because it has no storage-engine read phase. A
        cross-join of ``information_schema.columns`` against itself produces
        a long-running join the executor actually loops over.
        """
        import pymysql

        runner = build_sql_runner(mysql_url, statement_timeout_ms=500)
        sql = (
            "SELECT COUNT(*) FROM information_schema.columns a "
            "CROSS JOIN information_schema.columns b "
            "CROSS JOIN information_schema.columns c"
        )
        start = time.monotonic()
        # MAX_EXECUTION_TIME interruption surfaces as
        # ``pymysql.err.OperationalError`` (errno 3024). A bare ``Exception``
        # would also pass for unrelated failures and silently mis-pass when
        # the timeout never fires.
        with pytest.raises(pymysql.err.OperationalError):
            await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"timeout fired too slowly ({elapsed:.2f}s)"


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
