"""Unit tests for the read-only SQL guard."""

from __future__ import annotations

import pytest

from sqllens.safety.readonly import UnsafeSqlError, assert_select_only


class TestAcceptsRead:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM users",
            "SELECT a, b FROM t WHERE c > 5 ORDER BY a LIMIT 10",
            "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id",
            "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte",
            "SELECT 1 UNION SELECT 2",
            "SELECT 1 INTERSECT SELECT 1",
            "SELECT 1 EXCEPT SELECT 2",
        ],
    )
    def test_select_variants_pass(self, sql: str) -> None:
        assert_select_only(sql)


class TestRejectsWrites:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users VALUES (1, 'a')",
            "UPDATE users SET name = 'a' WHERE id = 1",
            "DELETE FROM users WHERE id = 1",
            "DROP TABLE users",
            "CREATE TABLE t (a INT)",
            "ALTER TABLE users ADD COLUMN x INT",
        ],
    )
    def test_dml_ddl_rejected(self, sql: str) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only(sql)


class TestRejectsMixed:
    def test_multiple_statements_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="single SQL statement"):
            assert_select_only("SELECT 1; DROP TABLE users")

    def test_empty_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="empty"):
            assert_select_only("")
        with pytest.raises(UnsafeSqlError, match="empty"):
            assert_select_only("   ")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only("not even sql")


class TestNestedDmlInCte:
    """sqlglot 25+ accepts ``WITH x AS (INSERT ...) SELECT * FROM x`` syntactically."""

    def test_insert_in_cte_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="nested INSERT"):
            assert_select_only(
                "WITH inserted AS ("
                "INSERT INTO log (msg) VALUES ('x') RETURNING id"
                ") SELECT * FROM inserted",
                dialect="postgres",
            )
