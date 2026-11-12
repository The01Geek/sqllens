# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``uses_table_shorthand`` finds ``TABLE t`` with the tokenizer, on any sqlglot parse."""

from __future__ import annotations

import logging

import pytest
import sqlglot

from sqllens.safety.readonly import uses_table_shorthand

DIALECTS = ["postgres", "mysql", "sqlite", None]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "TABLE users",
        "table users",
        "/* note */ TABLE users",
        "TABLE public.users",
        "SELECT 1; TABLE users",
        "SELECT * FROM (TABLE orders) sub",
        "SELECT * FROM LATERAL (TABLE orders) sub",
        "WITH x AS (TABLE users) SELECT id FROM x",
        "SELECT id FROM a UNION TABLE users",
        "SELECT id FROM a UNION ALL TABLE users",
        "SELECT id FROM a UNION DISTINCT TABLE users",
        "SELECT id FROM a INTERSECT TABLE users",
        "SELECT id FROM a EXCEPT TABLE users",
        "WITH x AS (SELECT 1 AS id) TABLE orders",
        "WITH RECURSIVE x AS (SELECT 1 AS id) TABLE orders",
        "INSERT INTO t TABLE users",
        "CREATE TABLE copy AS TABLE users",
        # A quoted identifier spelled like a command keyword exempts nothing.
        'SELECT * FROM "create" TABLE users',
        # Not shorthand, but refused: the check errs toward True.
        "SELECT * FROM TABLE(f())",
        "SELECT id FROM t AS table",
    ],
)
def test_table_shorthand_is_found(sql: str, dialect: str | None) -> None:
    assert uses_table_shorthand(sql, dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users",
        'SELECT "table" FROM t',
        'SELECT * FROM "table"',
        "SELECT 'TABLE users'",
        "SELECT id FROM t -- TABLE users",
        "SELECT t.table FROM t",
        "SHOW CREATE TABLE users",
        "CREATE TABLE copy AS SELECT * FROM users",
        "CREATE TEMPORARY TABLE x (a INT)",
        "DROP TABLE users",
        "SHOW TABLE STATUS",
        "EXPLAIN TABLE users",
        "EXPLAIN ANALYZE TABLE users",
        "SELECT id FROM information_schema.tables",
        "",
    ],
)
def test_other_sql_is_not_flagged(sql: str, dialect: str | None) -> None:
    assert not uses_table_shorthand(sql, dialect)


def test_token_error_gives_false() -> None:
    # The callers parse afterwards, and that parse fails closed.
    assert not uses_table_shorthand("TABLE users WHERE x = 'unterminated", "postgres")


def test_unknown_dialect_warns_and_uses_no_dialect(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sqllens.safety.readonly"):
        assert uses_table_shorthand("TABLE users", "no-such-dialect")
    assert "unknown sqlglot dialect" in caplog.text


def test_other_tokenizer_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: object, sql: str) -> list[object]:
        raise RuntimeError("tokenizer bug")

    monkeypatch.setattr(sqlglot.dialects.postgres.Postgres, "tokenize", boom)
    with pytest.raises(RuntimeError, match="tokenizer bug"):
        uses_table_shorthand("TABLE users", "postgres")
