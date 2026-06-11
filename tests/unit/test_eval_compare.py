# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :mod:`sqllens.eval.compare`."""

from __future__ import annotations

import pytest

from sqllens.eval.compare import Status, compare, normalize_sql


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


def test_actual_unparseable_classifies_as_changed() -> None:
    """A garbled agent answer is a regression signal, not a golden-file bug."""
    expected = "SELECT 1"
    actual = "@@@ definitely not sql @@@"
    assert compare(expected, actual, dialect="sqlite") is Status.CHANGED


def test_dialect_none_still_classifies() -> None:
    # ``None`` dialect must fall through to sqlglot's default without raising.
    assert compare("SELECT 1", "select 1", dialect=None) is Status.PASS


def test_quoted_identifiers_under_postgres_dialect() -> None:
    expected = 'SELECT "id" FROM "users"'
    actual = "SELECT id FROM users"
    # Postgres lowercases unquoted identifiers; quoted ones preserve case.
    # Both renderings refer to the same column/table here, but they are
    # *structurally* different — CHANGED is the correct verdict, not PASS.
    assert compare(expected, actual, dialect="postgres") is Status.CHANGED


def test_sqlalchemy_dialect_name_postgresql_normalises() -> None:
    """`DatabaseConfig.dialect` returns 'postgresql' from a postgresql:// URL.

    Without dialect-name mapping, sqlglot raises ``Unknown dialect 'postgresql'``
    and every Postgres case silently classifies as ERROR.
    """
    # PASS path — the mapping must be applied.
    assert compare("SELECT 1", "select 1", dialect="postgresql") is Status.PASS
    # CHANGED path — the mapping must still apply (otherwise both sides ERROR).
    assert (
        compare("SELECT id FROM users", "SELECT id FROM accounts", dialect="postgresql")
        is Status.CHANGED
    )


def test_sqlalchemy_dialect_name_mssql_normalises() -> None:
    """`DatabaseConfig.dialect` returns 'mssql' from mssql:// — sqlglot wants 'tsql'."""
    assert compare("SELECT 1", "select 1", dialect="mssql") is Status.PASS


def test_sqlalchemy_dialect_name_mariadb_normalises() -> None:
    """`DatabaseConfig.dialect` returns 'mariadb' from mariadb:// — sqlglot
    has no mariadb dialect, so the map translates to 'mysql'. Pins all three
    explicit entries of ``_SQLGLOT_DIALECT_MAP``.
    """
    assert compare("SELECT 1", "select 1", dialect="mariadb") is Status.PASS


def test_unmapped_dialect_passes_through() -> None:
    """A dialect name sqlglot already recognises must pass through unchanged."""
    assert compare("SELECT 1", "select 1", dialect="sqlite") is Status.PASS
    assert compare("SELECT 1", "select 1", dialect="mysql") is Status.PASS


def test_dialect_lookup_is_case_insensitive() -> None:
    """An upper-cased URL scheme (technically tolerated by SQLAlchemy) must
    still hit the mapping so a Postgres deployment with a ``PostgreSQL://``
    URL doesn't re-trigger the silent-ERROR mode the dialect map fixed.
    """
    assert compare("SELECT 1", "select 1", dialect="PostgreSQL") is Status.PASS
    assert compare("SELECT 1", "select 1", dialect="MSSQL") is Status.PASS


def test_actual_unterminated_string_classifies_as_changed() -> None:
    """``TokenError`` (lex-level garbage like an unterminated string literal)
    must be treated the same as ``ParseError`` — actual unparseable → CHANGED,
    not ERROR.

    ``TokenError`` is a sibling of ``ParseError`` under ``SqlglotError``, not
    a subclass. A narrow ``except ParseError`` was the iter-2 mistake; this
    test pins the broader ``SqlglotError`` catch so future regressions don't
    silently re-narrow it.
    """
    expected = "SELECT name FROM users"
    actual = 'SELECT name FROM users WHERE name = "unterminated'  # lex error
    assert compare(expected, actual, dialect="sqlite") is Status.CHANGED


def test_expected_unterminated_string_classifies_as_error() -> None:
    """The inverse: a tokenization error on the *expected* SQL is a golden-file
    bug — must classify as ERROR, not CHANGED, even though ``TokenError`` is
    not a ``ParseError`` subclass.
    """
    expected = 'SELECT * FROM users WHERE name = "unterminated'  # lex error
    actual = "SELECT * FROM users"
    assert compare(expected, actual, dialect="sqlite") is Status.ERROR


def test_unknown_dialect_raises_not_swallowed() -> None:
    """A dialect name sqlglot does NOT recognise (e.g. an unsupported third-party
    SQLAlchemy driver) must surface as a real exception, not be silently
    converted to ERROR — the operator needs the diagnostic, otherwise every
    case classifies as a misleading "golden file is malformed" ERROR.

    ``compare()`` catches only :class:`sqlglot.errors.SqlglotError`; a
    ``ValueError("Unknown dialect ...")`` is not a subclass of that family
    and propagates to ``_run_one_case``'s catch where it lands in
    :attr:`CaseResult.error` — see the matching runner-level test in
    ``test_eval_runner.py`` and the rationale in ``compare.py``'s docstring.
    """
    with pytest.raises(ValueError, match="Unknown dialect"):
        compare("SELECT 1", "select 1", dialect="cockroachdb_no_such")
