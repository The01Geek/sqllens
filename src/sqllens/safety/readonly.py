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


# DML/DDL node types refused anywhere in the tree. The ALTER node was renamed
# ``exp.AlterTable`` → ``exp.Alter`` partway through sqlglot's 25.x line, so a
# bare ``exp.Alter`` reference AttributeErrors on the low end of the pinned
# ``>=25.0,<26`` range (25.0.x ships only ``AlterTable``) while a bare
# ``exp.AlterTable`` AttributeErrors on 30.x. Resolve whichever the installed
# version exposes so the guard works across the whole pinned range.
_ALTER_TYPE: type[exp.Expression] | None = getattr(exp, "Alter", None) or getattr(
    exp, "AlterTable", None
)
_DML_DDL_TYPES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        _ALTER_TYPE,
    )
    if t is not None
)


# Per-dialect denylist of side-effecting / DoS / RCE functions. A syntactically
# valid ``SELECT`` that *calls* one of these passes the root-type / DML-DDL
# walk untouched, so without this the guard is RCE on the default SQLite
# deployment (``load_extension``) and a write/DoS vector on Postgres/MySQL.
# Names are matched case-insensitively against the parsed function node.
# CWE-89 / CWE-284 / CWE-770 / CWE-94.
_SIDE_EFFECT_FUNCS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"load_extension"}),
    "postgres": frozenset(
        {
            "dblink_exec",
            "pg_sleep",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "lo_import",
            "lo_export",
        }
    ),
    "mysql": frozenset({"sleep", "load_file", "benchmark"}),
}

# ``generate_series`` can enumerate billions of rows — a resource-exhaustion
# DoS independent of dialect (a row cap is a separate concern; *generating*
# the rows is itself the attack). sqlglot parses it as a known function class
# (``exp.GenerateSeries`` / ``exp.ExplodingGenerateSeries`` depending on
# dialect) rather than an ``exp.Anonymous`` node, so it is matched via the
# function class's ``sql_names()`` rather than a written name.
_ALWAYS_DENIED_FUNCS: frozenset[str] = frozenset(
    {"generate_series", "exploding_generate_series"}
)

# Resolved denylists, computed once at import (all inputs are module
# constants). ``assert_select_only`` runs on every generated query, so this
# stays off the hot path.
_DENIED_BY_DIALECT: dict[str, frozenset[str]] = {
    dialect: names | _ALWAYS_DENIED_FUNCS
    for dialect, names in _SIDE_EFFECT_FUNCS.items()
}
_DENIED_UNION: frozenset[str] = frozenset(_ALWAYS_DENIED_FUNCS).union(
    *_SIDE_EFFECT_FUNCS.values()
)

# Matching lower-cases the parsed function name, so every denylist entry must
# be lower-case or it is dead weight that silently fails open. Enforce at
# import (negligible cost) so a mixed-case typo fails fast instead of opening
# a hole.
assert _DENIED_UNION == frozenset(n.lower() for n in _DENIED_UNION), (
    "denylist entries must be lower-case"
)


def _denied_funcs(dialect: str | None) -> frozenset[str]:
    """Return the set of refused function names for ``dialect``.

    For an unknown/``None`` dialect we apply the **union** of every dialect's
    denylist — fail-closed, matching the existing "parse failure is unsafe"
    invariant.
    """
    return _DENIED_BY_DIALECT.get(dialect, _DENIED_UNION) if dialect else _DENIED_UNION


def _func_name_candidates(node: exp.Func) -> set[str]:
    """Lower-cased name(s) a function node could be denied under.

    Unknown functions parse as ``exp.Anonymous`` and carry their written name
    on ``.name``. Known functions (e.g. ``generate_series``) parse as a typed
    ``exp.Func`` subclass whose canonical name(s) come from ``sql_names()``.

    For a typed node we deliberately do NOT consult ``node.name``: on a typed
    ``exp.Func`` ``.name`` is the value of the node's first argument, not the
    function name (e.g. ``md5('pg_sleep')`` → ``.name == 'pg_sleep'``). Mixing
    that into the candidate set would reject benign reads whose string literal
    happens to equal a denied function name. ``sql_names()`` already yields the
    canonical name for every typed function we deny (e.g. ``GENERATE_SERIES``).
    """
    if isinstance(node, exp.Anonymous):
        return {node.name.lower()} if node.name else set()
    return {n.lower() for n in type(node).sql_names()}


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

    denied_funcs = _denied_funcs(dialect)

    # Reject DML/DDL nested anywhere in the tree (e.g. via CTEs). ``walk()``
    # yields bare ``exp.Expression`` nodes (sqlglot is pinned ``>=25.0,<26``;
    # the pre-v20 ``(node, parent, key)`` tuple form is out of range).
    for sub in stmt.walk():
        if isinstance(sub, _DML_DDL_TYPES):
            kind = type(sub).__name__.upper()
            raise UnsafeSqlError(f"only SELECT statements are allowed (found nested {kind})")
        # ``SELECT ... INTO new_tbl`` is a write on both Postgres and T-SQL —
        # sqlglot parses it as ``exp.Select`` with ``args["into"]`` set rather
        # than as ``exp.Create``, so the DML/DDL deny-walk above misses it.
        # Covers root-level and CTE-nested occurrences; ``INTO TEMP`` /
        # ``INTO UNLOGGED`` variants are the same node shape.
        if isinstance(sub, exp.Select) and sub.args.get("into"):
            raise UnsafeSqlError("SELECT ... INTO is not allowed (creates a table)")
        # Side-effecting / DoS / RCE function calls. A valid SELECT can still
        # call e.g. ``load_extension`` (SQLite RCE) or ``pg_read_file`` (data
        # exfiltration); the root-type/DML walk above does not see these.
        if isinstance(sub, exp.Func):
            offending = _func_name_candidates(sub) & denied_funcs
            if offending:
                name = sorted(offending)[0]
                raise UnsafeSqlError(
                    f"function {name!r} is not allowed: side-effecting / DoS / RCE "
                    "function refused by the read-only guard"
                )
