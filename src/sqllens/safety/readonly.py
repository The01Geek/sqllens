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
# query through the streaming/row-capped row-returning branch. Mirrors the SQL
# forms the readonly guard accepts (a SELECT, a CTE or set operation that
# ultimately yields rows, or a structural ``SHOW`` schema-discovery command —
# all of which return a result set rather than a rows-affected count). A
# first-token check is sufficient here because the readonly guard has already
# parsed and validated the statement upstream.
_READ_SHAPED_KEYWORDS: frozenset[str] = frozenset(
    {"SELECT", "WITH", "UNION", "INTERSECT", "EXCEPT", "SHOW"}
)


def first_sql_keyword(sql: str) -> str:
    """Return ``sql``'s leading keyword uppercased, or ``""`` if none.

    Skips a leading ``(`` so wrapped ``(WITH ... SELECT ...)`` / ``(SELECT
    ...)`` forms classify by their inner verb.
    """
    if not sql:
        return ""
    stripped = sql.strip()
    while stripped.startswith("("):
        stripped = stripped[1:].lstrip()
    return stripped.split(None, 1)[0].upper() if stripped else ""


def is_read_shaped(sql: str) -> bool:
    """Return True if ``sql``'s first keyword indicates a row-returning query.

    Cheap first-token check, intentionally lenient — used by SQL runners to
    decide whether to take the streaming ``fetchmany`` branch. The
    ``ReadOnlyGuardRunner`` (when enabled) has already enforced that the whole
    statement is read-only via sqlglot.
    """
    return first_sql_keyword(sql) in _READ_SHAPED_KEYWORDS


# Catalog schemas and object-name prefixes that mark a read as schema
# introspection rather than a data-answering query. The system prompt
# (agent/core/system_prompt/default.py) tells the agent to run these internally
# — a SELECT against information_schema / pg_catalog / sqlite_master, or a
# structural SHOW — to confirm a column/table name before retrying a failed
# query. Their success is NOT a recovered answer, so the MCP formatter must not
# treat such a card as one (see tools/_format.py).
_CATALOG_SCHEMAS: frozenset[str] = frozenset(
    {"information_schema", "pg_catalog", "mysql", "sys", "performance_schema"}
)
_CATALOG_NAME_PREFIXES: tuple[str, ...] = ("pg_", "sqlite_")


def is_introspection_query(sql: str, *, dialect: str | None = None) -> bool:
    """Return True if ``sql`` is a schema-introspection read, not a data query.

    Schema introspection = a structural ``SHOW`` (SHOW TABLES/COLUMNS/...), or a
    SELECT whose tables target a catalog (``information_schema``, ``pg_catalog``,
    ``sqlite_master``, ``pg_*`` system tables, ...). The agent runs these
    internally to confirm a column name before retrying a failed query, so their
    success is not a recovered answer for the user's question.

    Best-effort, fail-open-to-``False``: a query that does not parse (rare for
    one ``run_sql`` already executed) is treated as a data query, biasing toward
    surfacing a recovered answer rather than misclassifying a real one.
    """
    if first_sql_keyword(sql) == "SHOW":
        return True
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return False
    if parsed is None:
        return False
    for table in parsed.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        if schema in _CATALOG_SCHEMAS:
            return True
        if any(name.startswith(prefix) for prefix in _CATALOG_NAME_PREFIXES):
            return True
    return False


_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,  # SELECT ... UNION SELECT ...
    exp.Intersect,
    exp.Except,
    exp.With,  # CTE chain rooted at SELECT
)


# Structural ``SHOW`` commands the agent uses for schema discovery on a fresh
# database (no ChromaDB memory yet). These are read-only and disclose only the
# database's *structure* — the same information the agent already needs
# internally to write queries. Matched (fail-closed) against ``exp.Show``'s
# ``this`` subkind, uppercased. Anything not in this allowlist is rejected,
# notably the info-leaking variants ``SHOW GRANTS`` (permission disclosure),
# ``SHOW PROCESSLIST`` (cross-session SQL leak), ``SHOW {MASTER,SLAVE,REPLICA}
# STATUS`` (replication topology), and ``SHOW VARIABLES`` / ``SHOW STATUS``
# (server internals / secrets). ``SHOW`` only parses to ``exp.Show`` under the
# MySQL dialect; every other dialect falls back to an opaque ``exp.Command``
# rejected by the root-type gate, so this branch is MySQL-only in practice.
# sqlglot normalizes some spellings: ``SHOW SCHEMAS`` → ``DATABASES``. ``SHOW
# KEYS`` / ``SHOW FIELDS`` / ``SHOW INDEXES`` fall back to ``exp.Command`` and
# stay blocked — use the equivalent ``SHOW INDEX`` / ``SHOW COLUMNS``, which
# parse cleanly to ``exp.Show``.
_SAFE_SHOW_SUBKINDS: frozenset[str] = frozenset(
    {
        "TABLES",  # SHOW TABLES / SHOW FULL TABLES / SHOW TABLES LIKE ...
        "COLUMNS",  # SHOW COLUMNS / SHOW FULL COLUMNS FROM ...
        "DATABASES",  # SHOW DATABASES / SHOW SCHEMAS
        "INDEX",  # SHOW INDEX FROM ...
        "CREATE TABLE",  # SHOW CREATE TABLE ...
        "CREATE VIEW",  # SHOW CREATE VIEW ...
    }
)


# DML/DDL node types refused anywhere in the tree. The ALTER node was renamed
# ``exp.AlterTable`` → ``exp.Alter`` partway through sqlglot's 25.x line, so a
# bare ``exp.Alter`` reference AttributeErrors on the low end of the pinned
# ``>=25.0,<31`` range (25.0.x ships only ``AlterTable``) while a bare
# ``exp.AlterTable`` AttributeErrors on 30.x. Resolve whichever the installed
# version exposes so the guard works across the whole pinned range.
_ALTER_TYPE: type[exp.Expression] | None = getattr(exp, "Alter", None) or getattr(
    exp, "AlterTable", None
)
# Fail closed and loud: if a (future, post-pin-widening) sqlglot exposes
# neither name, a nested ALTER would silently slip the deny-walk (every other
# DML/DDL type is a bare attribute that AttributeErrors at import if missing —
# ALTER is the only one resolved dynamically, so it needs an explicit guard).
# Not an ``assert`` — that is stripped under ``python -O`` and this is a
# security invariant.
if _ALTER_TYPE is None:  # pragma: no cover - unreachable within the pinned range
    raise RuntimeError(
        "sqlglot exposes neither exp.Alter nor exp.AlterTable; "
        "the read-only guard cannot reject nested ALTER (fail-closed)"
    )
_DML_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    _ALTER_TYPE,
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
# a hole. An explicit raise, not an ``assert`` — ``assert`` is stripped under
# ``python -O`` and this is a security invariant, not a debug check.
if _DENIED_UNION != frozenset(n.lower() for n in _DENIED_UNION):
    raise RuntimeError("denylist entries must be lower-case (fail-closed)")


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
    # ``sql_names()`` is a sqlglot ``Func`` classmethod; guard defensively so a
    # future Func-shaped node lacking it degrades to "no candidates" (the node
    # simply isn't matched) rather than raising and turning every query into
    # the guard's fail-closed path.
    sql_names = getattr(type(node), "sql_names", None)
    if not callable(sql_names):
        return set()
    return {n.lower() for n in sql_names()}


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

    if isinstance(stmt, exp.Show):
        # Structural schema-discovery ``SHOW`` (MySQL). Fail-closed: only the
        # allowlisted subkinds pass; info-leaking variants (GRANTS, PROCESSLIST,
        # *STATUS, VARIABLES, ...) fall through to the rejection below. Falls
        # through to the shared deny-walk so a smuggled side-effecting function
        # (e.g. ``SHOW COLUMNS FROM t WHERE x = sleep(1)``) is still caught.
        subkind = str(stmt.args.get("this") or "").upper().strip()
        if subkind not in _SAFE_SHOW_SUBKINDS:
            shown = f"SHOW {subkind}".strip()
            raise UnsafeSqlError(
                f"only structural schema-discovery SHOW commands are allowed "
                f"(got {shown})"
            )
    elif not isinstance(stmt, _ALLOWED_ROOT_TYPES):
        kind = type(stmt).__name__.upper()
        raise UnsafeSqlError(f"only SELECT statements are allowed (got {kind})")

    denied_funcs = _denied_funcs(dialect)

    # Reject DML/DDL nested anywhere in the tree (e.g. via CTEs). ``walk()``
    # yields bare ``exp.Expression`` nodes (sqlglot is pinned ``>=25.0,<31``;
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
