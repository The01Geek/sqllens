# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Compare dialect-normalized SQL; structural equality is not semantic equality."""

from __future__ import annotations

from enum import StrEnum

import sqlglot
import sqlglot.errors

# Translate SQLAlchemy URL schemes to sqlglot dialect names.
_SQLGLOT_DIALECT_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "mssql": "tsql",
    "mariadb": "mysql",
}


def _resolve_sqlglot_dialect(dialect: str | None) -> str | None:
    if dialect is None:
        return None
    key = dialect.lower()
    return _SQLGLOT_DIALECT_MAP.get(key, key)


class Status(StrEnum):
    """Per-case verdict."""

    PASS = "PASS"
    CHANGED = "CHANGED"
    ERROR = "ERROR"


class SqlComparisonError(ValueError):
    """One side of a SQL comparison could not be parsed."""


def normalize_sql(sql: str, *, dialect: str | None = None) -> str:
    """Parse and re-render SQL with normalized unquoted identifiers and whitespace.

    Keyword casing comes from sqlglot's renderer; ``normalize=True`` normalizes
    unquoted identifier case. Quoting remains significant to string comparison.
    SQLAlchemy dialect names are translated before parsing and rendering.
    Parse errors and unknown-dialect errors propagate to the caller.
    """
    resolved = _resolve_sqlglot_dialect(dialect)
    parsed = sqlglot.parse_one(sql, read=resolved)
    return parsed.sql(dialect=resolved, normalize=True, pretty=False)


def compare_sql(expected: str, actual: str, *, dialect: str | None = None) -> Status:
    """Compare SQL, raising a diagnostic that identifies an unparseable side.

    A dialect configuration error propagates unchanged. A parse or tokenization
    failure becomes ``SqlComparisonError`` so the runner can show its cause.
    """
    try:
        expected_norm = normalize_sql(expected, dialect=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise SqlComparisonError("expected SQL in the golden file could not be parsed") from exc
    try:
        actual_norm = normalize_sql(actual, dialect=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise SqlComparisonError("actual SQL produced by the agent could not be parsed") from exc
    return Status.PASS if expected_norm == actual_norm else Status.CHANGED


def compare(expected: str, actual: str, *, dialect: str | None = None) -> Status:
    """Return PASS for equal normalized SQL, CHANGED for a difference, or ERROR
    when either side cannot be parsed. Unknown-dialect errors propagate.

    Call ``compare_sql`` when the caller also needs the parse-failure diagnostic.
    """
    try:
        return compare_sql(expected, actual, dialect=dialect)
    except SqlComparisonError:
        return Status.ERROR
