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

import logging

import sqlglot
from sqlglot import expressions as exp
from sqlglot.dialects.mysql import MySQL
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

logger = logging.getLogger("sqllens.safety.readonly")


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


def first_sql_keyword(sql: str, dialect: str | None = None) -> str:
    """Return ``sql``'s leading keyword uppercased, or ``""`` if none.

    The SQL is tokenized with sqlglot's tokenizer for ``dialect``, so comments
    and opening parentheses are skipped as that dialect's tokenizer skips them.
    The first remaining token is returned,
    uppercased, when it is a keyword or an unquoted identifier; a string,
    number, quoted identifier or symbol gives ``""``, and so does SQL with no
    remaining token (empty, whitespace-only, or only comments and opening
    parentheses). A dialect name sqlglot does not know is logged at
    ``WARNING`` and treated as no dialect.

    SQL the tokenizer rejects (``TokenError``, e.g. an unterminated string or
    comment) gives ``""``; any other exception from the tokenizer propagates.
    """
    try:
        resolved = sqlglot.Dialect.get_or_raise(dialect)
    except ValueError:
        logger.warning(
            "first_sql_keyword: unknown sqlglot dialect %r; tokenizing with no dialect",
            dialect,
        )
        resolved = sqlglot.Dialect.get_or_raise(None)
    try:
        tokens = resolved.tokenize(sql)
    except sqlglot.errors.TokenError:
        return ""
    keywords = resolved.tokenizer_class.KEYWORDS
    for token in tokens:
        if token.token_type is TokenType.L_PAREN:
            continue
        text = token.text.upper()
        if token.token_type is TokenType.VAR:
            return text
        # ``KEYWORDS`` also maps operators (``||``, ``::``); only a word is a
        # keyword here, so ``||`` must not be returned as one.
        is_word = text[:1].isalpha() or text[:1] == "_"
        if is_word and keywords.get(text) is token.token_type:
            return text
        return ""
    return ""


def is_read_shaped(sql: str, dialect: str | None = None) -> bool:
    """Return True if ``sql``'s first keyword indicates a row-returning query.

    Cheap first-token check (``first_sql_keyword`` under ``dialect``) — used by
    SQL runners to decide whether to take the streaming ``fetchmany`` branch.
    The ``ReadOnlyGuardRunner`` (when enabled) has already enforced that the
    whole statement is read-only via sqlglot.
    """
    return first_sql_keyword(sql, dialect) in _READ_SHAPED_KEYWORDS


def sqlglot_dialect_for_url(url: str) -> str | None:
    """Map a database URL to the sqlglot dialect name used by the safety guards.

    The dialect is the ``_CATALOGS_BY_DIALECT`` key the URL scheme starts with
    (``postgresql+psycopg`` → ``postgres``), so every dialect this returns has
    a catalog entry; any other scheme returns ``None``.
    """
    scheme = url.split("://", 1)[0].lower()
    for dialect in _CATALOGS_BY_DIALECT:
        if scheme.startswith(dialect):
            return dialect
    return None


# Catalog schemas and object-name prefixes that mark a read as schema
# introspection rather than a data-answering query. Keyed by sqlglot dialect:
# an engine-specific catalog counts only on its own engine, so a data table
# that merely starts with another engine's prefix (``pg_payments`` on MySQL)
# is not misread as a catalog. ``sqlglot_dialect_for_url`` derives its
# dialects from these keys; any other dialect falls back to the union.
_SHARED_CATALOG_SCHEMAS: frozenset[str] = frozenset({"information_schema"})
# dialect -> (catalog schemas, catalog object-name prefixes)
_CATALOGS_BY_DIALECT: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "postgres": (frozenset({"pg_catalog"}), ("pg_",)),
    "mysql": (frozenset({"mysql", "sys", "performance_schema"}), ()),
    "sqlite": (frozenset(), ("sqlite_", "pragma_")),
}
_ALL_CATALOG_SCHEMAS: frozenset[str] = _SHARED_CATALOG_SCHEMAS.union(
    *(schemas for schemas, _prefixes in _CATALOGS_BY_DIALECT.values())
)
_ALL_CATALOG_NAME_PREFIXES: tuple[str, ...] = tuple(
    prefix for _schemas, prefixes in _CATALOGS_BY_DIALECT.values() for prefix in prefixes
)


def _catalog_names(dialect: str | None) -> tuple[frozenset[str], tuple[str, ...]]:
    catalogs = _CATALOGS_BY_DIALECT.get(dialect) if dialect is not None else None
    if catalogs is None:
        return _ALL_CATALOG_SCHEMAS, _ALL_CATALOG_NAME_PREFIXES
    schemas, prefixes = catalogs
    return _SHARED_CATALOG_SCHEMAS | schemas, prefixes


def _cte_reference_ids(parsed: exp.Expression, dialect: str | None) -> set[int]:
    """Return the ids of ``exp.Table`` nodes scope analysis resolves to a CTE.

    Identifiers are normalized in place first, so a CTE reference resolves
    whatever its letter case where the dialect folds unquoted names. A table
    is a CTE reference when it has no schema or catalog qualifier and its own
    name (not its alias) is a CTE visible in its scope, so an aliased or
    schema-qualified catalog table beside a same-named CTE or derived table
    still counts. Any exception from normalization or scope analysis
    excludes nothing.
    """
    try:
        normalize_identifiers(parsed, dialect=dialect)
        references: set[int] = set()
        for scope in traverse_scope(parsed):
            references.update(
                id(table)
                for table in scope.tables
                if not table.args.get("db")
                and not table.args.get("catalog")
                and table.name in scope.cte_sources
            )
    except Exception as exc:
        logger.info(
            "is_introspection_query: identifier normalization or scope analysis "
            "failed under dialect %r (%s); "
            "counting every table reference",
            dialect,
            type(exc).__name__,
        )
        return set()
    return references


def is_introspection_query(sql: str, *, dialect: str | None = None) -> bool:
    """Return True if ``sql`` is a schema-introspection read, not a data query.

    Schema introspection = a statement whose ``first_sql_keyword`` under
    ``dialect`` is ``SHOW``, or a statement that references any catalog table,
    even alongside data tables. A reference that sqlglot's scope analysis
    resolves to a CTE is not a catalog table, whatever its name. The catalogs are
    ``information_schema`` for every dialect, plus the engine's own: the
    ``pg_catalog`` schema and ``pg_`` name prefix for ``postgres``; the
    ``mysql``, ``sys`` and ``performance_schema`` schemas for ``mysql``; the
    ``sqlite_`` and ``pragma_`` name prefixes for ``sqlite``. A name prefix
    matches, in any casing, a table name or a table-valued function called in
    ``FROM`` (``pragma_table_info('t')``). With no dialect, or one not listed,
    every engine's catalogs count; a dialect name sqlglot does not know is
    parsed as no dialect.

    The MCP formatter (``tools/_format.py``) uses this to withhold a catalog
    result from the user and to keep a catalog lookup's success from clearing
    an earlier query failure.

    Best-effort, fail-open-to-``False``: a query whose leading keyword is not
    ``SHOW`` and that does not parse under ``dialect`` (a sqlglot parse or
    tokenize error, or an empty parse) is treated as a data query, so its
    result is shown; a ``SHOW``-led statement returns ``True`` before any
    parse. Any other exception from
    the parser propagates, so the caller reports an error rather than guessing
    (``query_database`` returns its sanitized internal error). An exception
    from identifier normalization or scope analysis excludes no CTE reference.
    """
    if dialect is not None:
        try:
            sqlglot.Dialect.get_or_raise(dialect)
        except ValueError:
            logger.warning(
                "is_introspection_query: unknown sqlglot dialect %r; "
                "classifying with no dialect",
                dialect,
            )
            dialect = None
    if first_sql_keyword(sql, dialect) == "SHOW":
        return True
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        logger.info(
            "is_introspection_query: SQL did not parse under dialect %r (%s); "
            "classifying it as a data query",
            dialect,
            type(exc).__name__,
        )
        return False
    if parsed is None:
        logger.info(
            "is_introspection_query: SQL parsed to nothing under dialect %r; "
            "classifying it as a data query",
            dialect,
        )
        return False
    cte_references = _cte_reference_ids(parsed, dialect)
    schemas, prefixes = _catalog_names(dialect)
    for table in parsed.find_all(exp.Table):
        if id(table) in cte_references:
            continue
        schema = (table.db or "").lower()
        if schema in schemas:
            return True
        source = table.this
        name = source.name if isinstance(source, exp.Anonymous) else table.name
        if (name or "").lower().startswith(prefixes):
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


# DML/DDL node types refused anywhere in the tree.
_DML_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
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
            "dblink",
            "dblink_connect",
            "dblink_connect_u",
            "dblink_disconnect",
            "dblink_open",
            "dblink_fetch",
            "dblink_close",
            "dblink_send_query",
            "dblink_get_result",
            "dblink_get_notify",
            "dblink_cancel_query",
            "set_config",
            "pg_notify",
            "pg_reload_conf",
            "pg_promote",
            "pg_rotate_logfile",
            "pg_switch_wal",
            "pg_advisory_lock",
            "pg_advisory_lock_shared",
            "pg_advisory_xact_lock",
            "pg_advisory_xact_lock_shared",
            "pg_try_advisory_lock",
            "pg_try_advisory_lock_shared",
            "pg_try_advisory_xact_lock",
            "pg_try_advisory_xact_lock_shared",
            "pg_advisory_unlock",
            "pg_advisory_unlock_shared",
            "pg_advisory_unlock_all",
            "pg_stat_reset",
            "pg_stat_reset_shared",
            "pg_stat_reset_single_table_counters",
            "pg_stat_reset_single_function_counters",
            "pg_stat_reset_slru",
            "pg_stat_reset_replication_slot",
            "pg_stat_reset_subscription_stats",
            "pg_stat_reset_backend_stats",
            "query_to_xml",
            "query_to_xmlschema",
            "query_to_xml_and_xmlschema",
            "table_to_xml",
            "table_to_xmlschema",
            "table_to_xml_and_xmlschema",
            "schema_to_xml",
            "schema_to_xmlschema",
            "schema_to_xml_and_xmlschema",
            "database_to_xml",
            "database_to_xmlschema",
            "database_to_xml_and_xmlschema",
            "cursor_to_xml",
            "cursor_to_xmlschema",
            # Like ``query_to_xml``, these run query text passed as a string,
            # or read a relation named by a text argument, so the statement
            # they read is never part of the parsed tree. Blocking by name
            # covers every overload (e.g. the tsquery-only ``ts_rewrite``).
            # Text search: ts_stat, ts_rewrite. tablefunc: crosstab*,
            # connectby. xml2: xpath_table. dblink: dblink_build_sql_insert /
            # _update (copy a named table's row into the returned text).
            # pageinspect: get_raw_page, bt_page_items (raw page / index
            # contents of a named relation).
            "ts_stat",
            "ts_rewrite",
            "crosstab",
            "crosstab2",
            "crosstab3",
            "crosstab4",
            "connectby",
            "xpath_table",
            "dblink_build_sql_insert",
            "dblink_build_sql_update",
            "get_raw_page",
            "bt_page_items",
            "pg_sleep",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_file_write",
            "pg_file_rename",
            "pg_file_unlink",
            "pg_file_sync",
            "pg_logdir_ls",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            # Path / log file disclosure on Postgres (pg_stat_file has been
            # around since 9.x; pg_ls_logdir / pg_ls_waldir since 10;
            # pg_read_server_files since 11 — none are new in PG 14). Parse as
            # exp.Anonymous and would otherwise slip the guard, exposing the
            # on-disk WAL/log layout to anyone who can issue a SELECT.
            "pg_stat_file",
            "pg_read_server_files",
            "pg_ls_logdir",
            "pg_ls_waldir",
            "pg_ls_tmpdir",
            "pg_ls_archive_statusdir",
            "pg_ls_logicalmapdir",
            "pg_ls_logicalsnapdir",
            "pg_ls_replslotdir",
            "lo_import",
            "lo_export",
            "lo_create",
            "lo_creat",
            "lo_from_bytea",
            "lo_put",
            "lowrite",
            "lo_truncate",
            "lo_truncate64",
            "lo_unlink",
        }
    ),
    # ``sys_exec`` / ``sys_eval`` are lib_mysqludf_sys UDFs: where the UDF is
    # installed and the configured DB role has EXECUTE, they are an RCE seam.
    # They parse as exp.Anonymous and need explicit naming to stay refused.
    "mysql": frozenset(
        {
            "sleep",
            "load_file",
            "benchmark",
            "sys_exec",
            "sys_eval",
            "get_lock",
            "release_lock",
            "release_all_locks",
            "master_pos_wait",
            "source_pos_wait",
        }
    ),
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


def _mysql_parse_sql(sql: str, *, mysql_line_comments: bool) -> str:
    """Keep MySQL and parser token boundaries aligned, or refuse ambiguity.

    MySQL SQL modes can change quote escaping. Refuse backslash-escaped quote
    boundaries and use doubled quotes instead; ordinary backslashes are data.
    Reject non-comment double dashes and nested block-comment openers because
    sqlglot can discard SQL that MySQL would execute at those boundaries.
    """
    chars = list(sql)
    i = 0
    while i < len(sql):
        if sql[i] in "'\"`":
            quote = sql[i]
            i += 1
            while i < len(sql):
                if quote != "`" and sql[i] == "\\" and i + 1 < len(sql):
                    if sql[i + 1] == quote:
                        raise UnsafeSqlError("MySQL backslash-escaped quotes are not allowed")
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("/*", i):
            if sql.startswith(("/*!", "/*M!"), i):
                raise UnsafeSqlError("MySQL executable comments are not allowed")
            end = sql.find("*/", i + 2)
            if end < 0:
                raise UnsafeSqlError("Unterminated MySQL comment")
            if "/*" in sql[i + 2:end]:
                raise UnsafeSqlError("Nested MySQL comments are not allowed")
            i = end + 2
            continue
        if sql.startswith("--", i):
            following = sql[i + 2:i + 3]
            if following and not (ord(following) <= 32 or ord(following) == 127):
                raise UnsafeSqlError("Ambiguous MySQL double-dash syntax is not allowed")
        if sql[i] == "#" or sql.startswith("--", i):
            end = sql.find("\n", i)
            if end < 0:
                end = len(sql)
            # Normalize only comment text, never literals or identifiers.
            for offset in range(i, end):
                if chars[offset] == "\r":
                    if not mysql_line_comments and sql[offset:offset + 2] != "\r\n":
                        raise UnsafeSqlError(
                            "Ambiguous line-comment carriage return without a SQL dialect"
                        )
                    chars[offset] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _parse_statements(sql: str, dialect: str | None) -> list[exp.Expression | None]:
    """Parse ``sql`` as the guards see it, raising ``UnsafeSqlError`` if it cannot."""
    if not sql or not sql.strip():
        raise UnsafeSqlError("empty SQL")

    try:
        parser_dialect = sqlglot.Dialect.get_or_raise(dialect)
    except ValueError as e:
        raise UnsafeSqlError(f"could not resolve SQL dialect: {e}") from e

    try:
        if type(parser_dialect) in (MySQL, sqlglot.Dialect):
            sql = _mysql_parse_sql(sql, mysql_line_comments=type(parser_dialect) is MySQL)
        return sqlglot.parse(sql, dialect=parser_dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
        raise UnsafeSqlError(f"could not parse SQL: {e}") from e


def _refuse_denied_function(node: exp.Expression, denied_funcs: frozenset[str]) -> None:
    if isinstance(node, exp.Func):
        offending = _func_name_candidates(node) & denied_funcs
        if offending:
            name = sorted(offending)[0]
            raise UnsafeSqlError(
                f"function {name!r} is not allowed: side-effecting / DoS / RCE "
                "function refused by the read-only guard's function denylist"
            )


def assert_no_denied_functions(sql: str, *, dialect: str | None = None) -> None:
    """Raise ``UnsafeSqlError`` if ``sql`` calls a function on the denylist.

    This is the function-denylist part of :func:`assert_select_only` without
    its statement-type checks. It fails closed the same way: empty SQL, an
    unresolvable dialect, MySQL input whose token boundaries are ambiguous
    (``_mysql_parse_sql``), a parse or tokenize error, or an empty statement
    in the parse raises.
    """
    denied_funcs = _denied_funcs(dialect)
    for stmt in _parse_statements(sql, dialect):
        if stmt is None:
            raise UnsafeSqlError("empty parse tree")
        for sub in stmt.walk():
            _refuse_denied_function(sub, denied_funcs)


def assert_select_only(sql: str, *, dialect: str | None = None) -> None:
    """Raise ``UnsafeSqlError`` if ``sql`` contains anything other than reads.

    Multiple statements are not allowed. Parse failures are treated as unsafe —
    we'd rather block a query we can't understand than execute it.
    """
    statements = _parse_statements(sql, dialect)

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
    # yields bare ``exp.Expression`` nodes (sqlglot is pinned ``>=30.22,<31``;
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
        _refuse_denied_function(sub, denied_funcs)
