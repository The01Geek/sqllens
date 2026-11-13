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

from sqllens.safety.rls import RlsError

logger = logging.getLogger(__name__)

# Metadata-only system views the agent's schema introspection relies on. They
# describe structure, not stored rows. Statistics views that expose sampled
# column values (MySQL ``COLUMN_STATISTICS``) are deliberately absent and stay
# blocked like any other view.
_METADATA_VIEWS: dict[str, frozenset[tuple[str, str]]] = {
    "mysql": frozenset(
        ("information_schema", name) for name in (
            "schemata", "tables", "columns", "views", "statistics", "table_constraints",
            "key_column_usage", "referential_constraints", "check_constraints",
        )
    ),
}

# Relation kinds in each dialect's own catalog vocabulary: SQLite
# ``pragma_table_list.type`` and MySQL ``information_schema.TABLES.TABLE_TYPE``.
# Anything outside both sets is refused.
_VIEW_KINDS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"view"}),
    "mysql": frozenset({"VIEW", "SYSTEM VIEW"}),
}
_BASE_TABLE_KINDS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"table"}),
    "mysql": frozenset({"BASE TABLE"}),
}

_SQLITE_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)


def _relations(sql: str, dialect: str) -> list[exp.Table]:
    tree = sqlglot.parse_one(sql, dialect=dialect)
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
    written, lower-cased and ASCII-lower-cased, so a lookup matches whether or
    not the server folds non-ASCII letters.
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
    schema introspection keeps working; every other view is refused. Under
    dialect ``mysql`` a name in call position that matches, ignoring case, a
    MySQL stored function is refused, whether or not the statement references
    a relation. A dialect with no entry here refuses any statement that
    references a relation, before querying the connection.
    """
    try:
        relations = _relations(sql, dialect)
        called = _called_names(sql, dialect) if dialect == "mysql" else []
        if not relations and not called:
            return
        cursor = connection.cursor()
        try:
            if relations:
                _verify_relations(cursor, relations, dialect)
            if called:
                _refuse_database_routines(cursor, called)
        finally:
            cursor.close()
    except RlsError:
        raise
    except Exception as exc:
        # The client sees only the sanitized message; log the cause so a guard
        # bug, a revoked catalog grant and a lost connection stay distinguishable.
        # SQLite runs no function-call check, so its message names relations only.
        checked = "relation types or function calls" if dialect == "mysql" else "relation types"
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


def _refuse_database_routines(cursor: Any, names: list[str]) -> None:
    """Refuse a call whose name matches a MySQL stored function.

    Checks stored functions in ``information_schema.ROUTINES``. Matching is by
    name alone across schemas, so a built-in call is refused too when such a
    routine shares its name.
    """
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
