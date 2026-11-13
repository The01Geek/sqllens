# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :mod:`sqllens.eval.compare`."""

from __future__ import annotations

import pytest

from sqllens.eval.compare import Status, compare, compare_sql, normalize_sql


def test_normalize_collapses_whitespace_and_casing() -> None:
    a = "select   *  FROM  users  WHERE  id=1"
    b = "SELECT * FROM users WHERE id = 1"
    assert normalize_sql(a, dialect="sqlite") == normalize_sql(b, dialect="sqlite")


def test_pass_on_casing_only_diff() -> None:
    expected = "SELECT count(*) FROM users"
    actual = "select COUNT(*) from users"
    assert compare(expected, actual, dialect="sqlite") is Status.PASS


def test_pass_on_whitespace_only_diff() -> None:
    expected = "SELECT id, name FROM users WHERE active = 1"
    actual = "SELECT    id,name    FROM users WHERE  active=1"
    assert compare(expected, actual, dialect="sqlite") is Status.PASS


def test_changed_on_different_structure() -> None:
    expected = "SELECT id FROM users WHERE active = 1"
    actual = "SELECT id FROM users WHERE active = 1 AND deleted = 0"
    assert compare(expected, actual, dialect="sqlite") is Status.CHANGED


def test_changed_on_different_table() -> None:
    expected = "SELECT id FROM users"
    actual = "SELECT id FROM accounts"
    assert compare(expected, actual, dialect="sqlite") is Status.CHANGED


def test_error_when_expected_unparseable() -> None:
    expected = "this is not sql ## ?? !!"
    actual = "SELECT 1"
    # The golden file is malformed — surface ERROR, not "every case CHANGED".
    assert compare(expected, actual, dialect="sqlite") is Status.ERROR


def test_actual_unparseable_classifies_as_error() -> None:
    """Unparseable agent output follows the issue's ERROR taxonomy."""
    expected = "SELECT 1"
    actual = "@@@ definitely not sql @@@"
    assert compare(expected, actual, dialect="sqlite") is Status.ERROR


def test_dialect_none_still_classifies() -> None:
    # ``None`` dialect must fall through to sqlglot's default without raising.
    assert compare("SELECT 1", "select 1", dialect=None) is Status.PASS


def test_quoted_identifiers_under_mysql_dialect() -> None:
    expected = "SELECT `id` FROM `users`"
    actual = "SELECT id FROM users"
    # Both renderings refer to the same column/table here, but they are
    # *structurally* different — CHANGED is the correct verdict, not PASS.
    assert compare(expected, actual, dialect="mysql") is Status.CHANGED


@pytest.mark.parametrize("compare_fn", [compare, compare_sql])
def test_postgresql_dialect_name_is_unknown(compare_fn) -> None:
    """``postgresql`` maps to no sqlglot dialect, so sqlglot's unknown-dialect
    ``ValueError`` propagates and the eval case reports an error."""
    with pytest.raises(ValueError, match="Unknown dialect 'postgresql'"):
        compare_fn("SELECT 1", "select 1", dialect="postgresql")


def test_sqlalchemy_dialect_name_mssql_normalises() -> None:
    """`DatabaseConfig.dialect` returns 'mssql' from mssql:// — sqlglot wants 'tsql'."""
    assert compare("SELECT 1", "select 1", dialect="mssql") is Status.PASS


def test_sqlalchemy_dialect_name_mariadb_normalises() -> None:
    """`DatabaseConfig.dialect` returns 'mariadb' from mariadb:// — sqlglot
    has no mariadb dialect, so the map translates to 'mysql'. With the mssql
    test above, pins every explicit entry of ``_SQLGLOT_DIALECT_MAP``.
    """
    assert compare("SELECT 1", "select 1", dialect="mariadb") is Status.PASS


def test_unmapped_dialect_passes_through() -> None:
    """A dialect name sqlglot already recognises must pass through unchanged."""
    assert compare("SELECT 1", "select 1", dialect="sqlite") is Status.PASS
    assert compare("SELECT 1", "select 1", dialect="mysql") is Status.PASS


def test_dialect_lookup_is_case_insensitive() -> None:
    """The SQLAlchemy-to-sqlglot lookup is case-insensitive."""
    assert compare("SELECT 1", "select 1", dialect="MariaDB") is Status.PASS
    assert compare("SELECT 1", "select 1", dialect="MSSQL") is Status.PASS


def test_actual_unterminated_string_classifies_as_error() -> None:
    """Tokenization and grammar errors both classify as ERROR."""
    expected = "SELECT name FROM users"
    actual = 'SELECT name FROM users WHERE name = "unterminated'  # lex error
    assert compare(expected, actual, dialect="sqlite") is Status.ERROR


def test_expected_unterminated_string_classifies_as_error() -> None:
    """The inverse: a tokenization error on the *expected* SQL is a golden-file
    bug — must classify as ERROR, not CHANGED, even though ``TokenError`` is
    not a ``ParseError`` subclass.
    """
    expected = 'SELECT * FROM users WHERE name = "unterminated'  # lex error
    actual = "SELECT * FROM users"
    assert compare(expected, actual, dialect="sqlite") is Status.ERROR


def test_unknown_dialect_raises_not_swallowed() -> None:
    """Unknown dialects retain their diagnostic instead of a bare ERROR status."""
    with pytest.raises(ValueError, match="Unknown dialect"):
        compare("SELECT 1", "select 1", dialect="cockroachdb_no_such")


@pytest.mark.parametrize(
    "expected, actual, status",
    [
        ("SELECT 1; SELECT 2", "SELECT 1; SELECT 3", Status.CHANGED),
        ("SELECT 1; SELECT 2", "select 1 ; select 2;", Status.PASS),
        ("SELECT 1", "SELECT 1; SELECT 2", Status.CHANGED),
        ("SELECT 1; SELECT 2", "SELECT 1; SELECT (", Status.ERROR),
        ("SELECT 1; SELECT (", "SELECT 1; SELECT 2", Status.ERROR),
        ("", "SELECT 1", Status.ERROR),
    ],
)
def test_comparison_covers_every_statement(expected: str, actual: str, status: Status) -> None:
    assert compare(expected, actual, dialect="sqlite") is status
