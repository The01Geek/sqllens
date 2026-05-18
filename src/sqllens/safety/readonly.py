# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Read-only SQL enforcement.

The agent generates SQL from natural language; before that SQL is executed, we
parse it with sqlglot and reject anything that isn't a single ``SELECT`` (or a
``WITH ... SELECT``). This is defence in depth — the database user *should*
already lack write privileges, but we don't trust either side alone.

Rejecting at parse time means the LLM never sees an "executed mutation" code
path, which makes prompt-injection attacks harder.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp


class UnsafeSqlError(Exception):
    """Raised when SQL fails the read-only check."""


# First-keyword set used by per-runner adapters to decide whether to route a
# query through the streaming/row-capped SELECT branch. Mirrors the SQL forms
# the readonly guard accepts as "SELECT-shaped" (a CTE or set operation that
# ultimately yields rows). A first-token check is sufficient here because the
# readonly guard has already parsed and validated the statement upstream.
_READ_SHAPED_KEYWORDS: frozenset[str] = frozenset(
    {"SELECT", "WITH", "UNION", "INTERSECT", "EXCEPT"}
)


def is_read_shaped(sql: str) -> bool:
    """Return True if ``sql``'s first keyword indicates a row-returning query.

    Cheap first-token check, intentionally lenient — used by SQL runners to
    decide whether to take the streaming ``fetchmany`` branch. The
    ``ReadOnlyGuardRunner`` (when enabled) has already enforced that the whole
    statement is read-only via sqlglot.
    """
    if not sql:
        return False
    stripped = sql.strip()
    if not stripped:
        return False
    # Skip a leading "(" so "(WITH ... SELECT ...)" and "(SELECT ...)" forms
    # still classify as read-shaped.
    while stripped.startswith("("):
        stripped = stripped[1:].lstrip()
    first = stripped.split(None, 1)[0].upper() if stripped else ""
    return first in _READ_SHAPED_KEYWORDS


_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,  # SELECT ... UNION SELECT ...
    exp.Intersect,
    exp.Except,
    exp.With,  # CTE chain rooted at SELECT
)


def assert_select_only(sql: str, *, dialect: str | None = None) -> None:
    """Raise ``UnsafeSqlError`` if ``sql`` contains anything other than reads.

    Multiple statements are not allowed. Parse failures are treated as unsafe —
    we'd rather block a query we can't understand than execute it.
    """
    if not sql or not sql.strip():
        raise UnsafeSqlError("empty SQL")

    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        raise UnsafeSqlError(f"could not parse SQL: {e}") from e

    if len(statements) != 1:
        raise UnsafeSqlError(
            f"only a single SQL statement is allowed (got {len(statements)})"
        )

    stmt = statements[0]
    if stmt is None:
        raise UnsafeSqlError("empty parse tree")

    if not isinstance(stmt, _ALLOWED_ROOT_TYPES):
        kind = type(stmt).__name__.upper()
        raise UnsafeSqlError(f"only SELECT statements are allowed (got {kind})")

    # Reject DML/DDL nested anywhere in the tree (e.g. via CTEs).
    for node in stmt.walk():
        # sqlglot's walk yields (node, parent, key) in older versions and
        # bare nodes in newer ones. Normalize.
        sub = node[0] if isinstance(node, tuple) else node
        if isinstance(sub, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter)):
            kind = type(sub).__name__.upper()
            raise UnsafeSqlError(f"only SELECT statements are allowed (found nested {kind})")
        # ``SELECT ... INTO new_tbl`` is a write on both Postgres and T-SQL —
        # sqlglot parses it as ``exp.Select`` with ``args["into"]`` set rather
        # than as ``exp.Create``, so the DML/DDL deny-walk above misses it.
        # Covers root-level and CTE-nested occurrences; ``INTO TEMP`` /
        # ``INTO UNLOGGED`` variants are the same node shape.
        if isinstance(sub, exp.Select) and sub.args.get("into"):
            raise UnsafeSqlError("SELECT ... INTO is not allowed (creates a table)")
