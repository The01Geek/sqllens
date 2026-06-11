# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""SQL-text comparator for ``sqllens verify-memory``.

Two queries are PASS when their ``sqlglot``-normalised forms compare equal
under the configured database dialect: parse, re-render with consistent
keyword casing and whitespace, then ``==``. Two queries are CHANGED when both
parse but the normalised forms differ. ERROR is reserved for the case where
the *expected* SQL itself fails to parse — a bug in the golden file — so the
operator notices it instead of seeing every case silently CHANGE.

This is the only place that knows the PASS / CHANGED / ERROR taxonomy: the
runner depends on :func:`compare` and the :class:`Status` enum, nothing else.
A future ``execute_and_compare_rows`` comparator can be added alongside this
file without rewriting the runner — the runner reads the comparator's signature
``(expected, actual, *, dialect) -> Status``, and a row-based variant matches it.

Limitation explicitly accepted (and called out in the CLI help): normalised-SQL
comparison cannot prove *semantic* equivalence. Two queries with different
JOIN order, or ``IN`` vs ``EXISTS``, are correct against the same database but
register as CHANGED. CHANGED therefore means "the agent's SQL changed — review
it," not "definitely wrong." The PR's design decision (#208) accepts that
trade-off in exchange for not needing a live DB at verify time.
"""

from __future__ import annotations

from enum import StrEnum

import sqlglot
import sqlglot.errors

# Map ``DatabaseConfig.dialect`` (the SQLAlchemy URL scheme) to the name
# ``sqlglot`` accepts. Mismatched names raise ``ValueError("Unknown dialect
# 'postgresql'. Did you mean postgres?")`` from ``sqlglot.parse_one``, which
# the comparator would otherwise silently classify as ERROR for every case
# on the affected database — a Day-1 silent failure on Postgres. Unmapped
# schemes are passed through unchanged (e.g. ``sqlite``, ``mysql``,
# ``snowflake``, ``duckdb``, ``bigquery``, ``oracle`` all match).
_SQLGLOT_DIALECT_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "mssql": "tsql",
    "mariadb": "mysql",
}


def _resolve_sqlglot_dialect(dialect: str | None) -> str | None:
    """Translate a ``DatabaseConfig.dialect`` value to the sqlglot-recognised name.

    Lower-cases the input before lookup so a URL scheme written
    ``Postgresql://...`` or ``PostgreSQL://...`` (technically tolerated by
    SQLAlchemy) maps the same way as the conventional lowercase form.
    """
    if dialect is None:
        return None
    key = dialect.lower()
    return _SQLGLOT_DIALECT_MAP.get(key, key)


class Status(StrEnum):
    """Per-case verdict.

    String-valued so ``status.value`` renders cleanly into table output and JSON
    without an explicit ``.name`` lookup.
    """

    PASS = "PASS"
    CHANGED = "CHANGED"
    ERROR = "ERROR"


def normalize_sql(sql: str, *, dialect: str | None = None) -> str:
    """Re-render ``sql`` through ``sqlglot`` to canonical form under ``dialect``.

    Parse and render with the same dialect, normalised keyword casing
    (uppercase via ``normalize=True``), and single-line whitespace
    (``pretty=False``). Identifier casing is left to the dialect — sqlglot
    quotes case-sensitive identifiers when required by the target.

    Raises :class:`sqlglot.errors.ParseError` (and other ``sqlglot`` errors)
    on un-parseable input. Callers handle the failure rather than this helper
    swallowing it — see :func:`compare`.

    ``dialect`` is translated through :data:`_SQLGLOT_DIALECT_MAP` because
    ``DatabaseConfig.dialect`` (the SQLAlchemy URL scheme) uses names
    ``sqlglot`` does not all recognise (e.g. ``postgresql`` vs sqlglot's
    ``postgres``). Without the translation, every comparison against a
    Postgres-shaped config silently classifies as ERROR.
    """
    resolved = _resolve_sqlglot_dialect(dialect)
    parsed = sqlglot.parse_one(sql, read=resolved)
    return parsed.sql(dialect=resolved, normalize=True, pretty=False)


def compare(
    expected: str, actual: str, *, dialect: str | None = None
) -> Status:
    """Classify the agent's generated SQL against the golden expected SQL.

    Returns :class:`Status.PASS` when the two normalise to the same string,
    :class:`Status.CHANGED` when both parse but normalise differently, and
    :class:`Status.ERROR` when the *expected* SQL fails to parse (a bug in the
    golden file). When the *actual* SQL fails to parse the result is CHANGED,
    not ERROR — the agent produced something the comparator can't normalise but
    the case itself is well-formed; "the agent changed its output" is the
    correct read.

    Only :class:`sqlglot.errors.ParseError` is caught here. Other exceptions
    (notably ``ValueError("Unknown dialect '...'")`` from
    :func:`sqlglot.parse_one` when the configured dialect is not in
    :data:`_SQLGLOT_DIALECT_MAP` and not recognised by sqlglot directly) must
    propagate to the runner's per-case ``except`` so the operator sees the
    real cause in ``CaseResult.error`` rather than a misleading ERROR row
    with no diagnostic — see CLAUDE.md "structured signal, never silent
    success" and the iter-2 silent-failure review for the failure mode.

    ``dialect`` should be the value of :attr:`sqllens.config.DatabaseConfig.dialect`
    (e.g. ``"sqlite"``, ``"postgresql"``) so two valid renderings under the
    configured database compare equal. The name is translated through
    :data:`_SQLGLOT_DIALECT_MAP` before reaching ``sqlglot`` — see
    :func:`normalize_sql`. ``None`` is accepted and falls through to
    ``sqlglot``'s default behaviour.
    """
    try:
        expected_norm = normalize_sql(expected, dialect=dialect)
    except sqlglot.errors.ParseError:
        return Status.ERROR
    try:
        actual_norm = normalize_sql(actual, dialect=dialect)
    except sqlglot.errors.ParseError:
        return Status.CHANGED
    return Status.PASS if expected_norm == actual_norm else Status.CHANGED
