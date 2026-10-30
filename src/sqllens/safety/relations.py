# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Verify RLS query relations using the connection that will execute them."""

from __future__ import annotations

import itertools
import logging
from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from sqllens.safety.rls import RlsError, normalize_rls_identifiers

logger = logging.getLogger(__name__)

# Metadata-only system views the agent's schema introspection relies on. They
# describe structure, not stored rows. Statistics views that expose sampled
# column values (PostgreSQL ``pg_stats``, MySQL ``COLUMN_STATISTICS``) are
# deliberately absent and stay blocked like any other view.
_METADATA_VIEWS: dict[str, frozenset[tuple[str, str]]] = {
    "postgres": frozenset(
        [("information_schema", name) for name in (
            "schemata", "tables", "columns", "views", "table_constraints",
            "key_column_usage", "referential_constraints", "constraint_column_usage",
            "check_constraints",
        )]
        + [("pg_catalog", name) for name in ("pg_tables", "pg_views", "pg_indexes")]
    ),
    "mysql": frozenset(
        ("information_schema", name) for name in (
            "schemata", "tables", "columns", "views", "statistics", "table_constraints",
            "key_column_usage", "referential_constraints", "check_constraints",
        )
    ),
}

# Relation kinds in each dialect's own catalog vocabulary: SQLite
# ``pragma_table_list.type``, PostgreSQL ``pg_class.relkind`` and MySQL
# ``information_schema.TABLES.TABLE_TYPE``. Anything outside both sets is refused.
_VIEW_KINDS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"view"}),
    "postgres": frozenset({"v", "m"}),
    "mysql": frozenset({"VIEW", "SYSTEM VIEW"}),
}
_BASE_TABLE_KINDS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"table"}),
    "postgres": frozenset({"r", "p"}),
    "mysql": frozenset({"BASE TABLE"}),
}

_SQLITE_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)


def _relations(sql: str, dialect: str) -> list[exp.Table]:
    tree = sqlglot.parse_one(sql, dialect=dialect)
    normalize_rls_identifiers(tree, dialect)
    sources: dict[str, exp.Table] = {}
    accounted: set[int] = set()
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                if not isinstance(source.this, exp.Identifier):
                    raise RlsError("row-level security cannot verify this relation shape")
                relation = exp.Table(**{
                    key: source.args[key].copy()
                    for key in ("this", "db", "catalog") if source.args.get(key) is not None
                })
                sources[relation.sql(dialect=dialect)] = relation
        # Scope analysis resolves CTE/derived aliases without treating them as
        # database objects. Reject any table outside its accounted query scopes.
        accounted.update(id(table) for table in scope.tables)
    if any(id(table) not in accounted for table in tree.find_all(exp.Table)):
        raise RlsError("row-level security cannot verify this relation shape")
    return list(sources.values())


def _called_names(sql: str, dialect: str) -> list[str]:
    """Names written in call position (``name(`` or ``schema.name(``) in ``sql``.

    Read from tokens rather than typed function nodes: sqlglot renames many
    built-ins (``strpos`` parses to ``StrPosition``), and a keyword such as
    ``replace`` is a call name too. Keyword and type names before ``(``
    (``IN (``, ``DECIMAL(``) are collected as well. Each name is returned as
    written, lower-cased and ASCII-lower-cased, since PostgreSQL's folding of
    non-ASCII letters in unquoted names depends on the server encoding.
    """
    parser_dialect = sqlglot.Dialect.get_or_raise(dialect)
    keywords = type(parser_dialect).tokenizer_class.KEYWORDS
    tokens = parser_dialect.tokenize(sql)
    names: set[str] = set()
    for token, following in itertools.pairwise(tokens):
        if following.token_type != TokenType.L_PAREN or not token.text:
            continue
        if token.token_type in (TokenType.VAR, TokenType.IDENTIFIER) or (
            token.text.upper() in keywords
        ):
            text = token.text
            ascii_lower = "".join(ch.lower() if ch.isascii() else ch for ch in text)
            names.update((text, text.lower(), ascii_lower))
    return sorted(names)


def verify_base_tables(connection: Any, sql: str, *, dialect: str) -> None:
    """Refuse views, child tables, database routines and failed lookups under RLS.

    The caller supplies its live execution connection. No catalog result is
    cached across queries; CTE names are resolved by SQL scope analysis.
    Metadata-only system views in ``_METADATA_VIEWS`` remain readable so
    schema introspection keeps working; every other view is refused. On
    PostgreSQL a relation recorded as a child in ``pg_inherits`` (a partition
    or an inheritance child) is refused. On PostgreSQL and MySQL a name in call
    position that matches, ignoring case, a routine defined in the database is
    refused, whether or not the statement references a relation.
    """
    try:
        relations = _relations(sql, dialect)
        called = _called_names(sql, dialect) if dialect in ("postgres", "mysql") else []
        if not relations and not called:
            return
        cursor = connection.cursor()
        try:
            if relations:
                _verify_relations(cursor, relations, dialect)
            if called:
                _refuse_database_routines(cursor, called, dialect)
        finally:
            cursor.close()
    except RlsError:
        raise
    except Exception as exc:
        # The client sees only the sanitized message; log the cause so a guard
        # bug, a revoked catalog grant and a lost connection stay distinguishable.
        # SQLite runs no function-call check, so its message names relations only.
        checked = (
            "relation types or function calls"
            if dialect in ("postgres", "mysql")
            else "relation types"
        )
        logger.warning(
            "row-level-security check of %s raised an unexpected %s; failing closed",
            checked,
            type(exc).__name__,
            exc_info=True,
        )
        raise RlsError(
            f"row-level security could not verify {checked}; query blocked"
        ) from exc


def _verify_relations(cursor: Any, relations: list[exp.Table], dialect: str) -> None:
    """Raise ``RlsError`` for a relation that is not a verified base table.

    A lookup that raises, or a non-boolean child flag, propagates as another
    exception, which the caller, ``verify_base_tables``, logs before refusing the
    query.
    """
    mysql_fold = False
    if dialect == "mysql":
        cursor.execute("SELECT @@lower_case_table_names")
        mode = cursor.fetchone()
        if mode is None or mode[0] not in (0, 1, 2):
            raise RlsError("row-level security could not verify identifier matching")
        mysql_fold = mode[0] != 0
    for relation in relations:
        if relation.catalog:
            raise RlsError("row-level security cannot verify catalog-qualified relations")
        if dialect == "sqlite":
            kind = _sqlite_kind(cursor, relation)
        elif dialect == "postgres":
            # Match the allowlist on the namespace the connection
            # resolved, so an unqualified name shadowed via search_path
            # cannot borrow a system view's exemption.
            cursor.execute(
                "SELECT c.relkind, n.nspname, c.relname, EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid = c.oid) "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.oid = pg_catalog.to_regclass(%s)",
                (relation.sql(dialect="postgres"),),
            )
            row = cursor.fetchone()
            kind = row[0] if row else None
            if kind == "v" and tuple(row[1:3]) in _METADATA_VIEWS["postgres"]:
                continue
            if kind in _BASE_TABLE_KINDS["postgres"]:
                is_child = row[3]
                if not isinstance(is_child, bool):
                    # Raised as an unexpected error so the caller logs it and
                    # refuses with the sanitized lookup-failure message.
                    raise TypeError(
                        f"pg_inherits child flag is {type(is_child).__name__}, not bool"
                    )
                if is_child:
                    raise RlsError(
                        "partition and inheritance child tables are not supported while "
                        "row-level security is active; query the top-level table"
                    )
        elif dialect == "mysql":
            # information_schema cannot hold user objects and its names
            # are case-insensitive, so a qualified name identifies it.
            if (
                (relation.db or "").lower(), relation.name.lower()
            ) in _METADATA_VIEWS["mysql"]:
                continue
            schema, name = "TABLE_SCHEMA", "TABLE_NAME"
            schema_arg, name_arg = "COALESCE(%s, DATABASE())", "%s"
            if mysql_fold:
                schema, name = f"LOWER({schema})", f"LOWER({name})"
                schema_arg, name_arg = f"LOWER({schema_arg})", f"LOWER({name_arg})"
            cursor.execute(
                "SELECT TABLE_TYPE FROM information_schema.TABLES "
                f"WHERE BINARY {schema} = BINARY {schema_arg} "
                f"AND BINARY {name} = BINARY {name_arg}",
                (relation.db or None, relation.name),
            )
            row = cursor.fetchone()
            kind = row[0] if row else None
        else:
            raise RlsError("row-level security cannot verify this database dialect")
        if kind in _VIEW_KINDS[dialect]:
            raise RlsError(
                "database views are not supported while row-level security is active"
            )
        if kind not in _BASE_TABLE_KINDS[dialect]:
            raise RlsError(
                "row-level security could not verify a base table; query blocked"
            )


def _refuse_database_routines(cursor: Any, names: list[str], dialect: str) -> None:
    """Refuse a call whose name matches a routine defined in the database.

    PostgreSQL checks ``pg_proc`` outside ``pg_catalog`` and
    ``information_schema``; MySQL checks stored functions in
    ``information_schema.ROUTINES``. Matching is by name alone across schemas
    and argument signatures, so a built-in call is refused too when such a
    routine shares its name.
    """
    if dialect == "postgres":
        cursor.execute(
            "SELECT p.proname FROM pg_catalog.pg_proc p "
            "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND (p.proname::text = ANY(%s) OR lower(p.proname::text) = ANY(%s)) "
            "LIMIT 1",
            (names, names),
        )
    else:
        placeholders = ", ".join(["%s"] * len(names))
        cursor.execute(
            "SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
            "WHERE ROUTINE_TYPE = 'FUNCTION' "
            f"AND LOWER(ROUTINE_NAME) IN ({placeholders}) LIMIT 1",
            tuple(names),
        )
    row = cursor.fetchone()
    if row is not None:
        raise RlsError(
            "functions defined in the database are not supported while "
            "row-level security is active; a call in this query matches the database "
            f"function {str(row[0])!r} (names are compared ignoring case)"
        )


def _sqlite_kind(cursor: Any, relation: exp.Table) -> str | None:
    if relation.name.lower() in _SQLITE_SCHEMA_TABLES:
        return "table"
    if relation.db:
        schemas = [relation.db]
    else:
        cursor.execute("PRAGMA database_list")
        schemas = ["temp", "main"] + [row[1] for row in cursor.fetchall() if row[1] != "main"]
    for schema in schemas:
        cursor.execute(
            "SELECT type FROM pragma_table_list(?) WHERE schema = ? COLLATE NOCASE",
            (relation.name, schema),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    return None
