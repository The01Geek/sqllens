# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Verify RLS query relations using the connection that will execute them."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import traverse_scope

from sqllens.safety.rls import RlsError, normalize_rls_identifiers

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


def verify_base_tables(connection: Any, sql: str, *, dialect: str) -> None:
    """Refuse views, unknown relation kinds, and failed catalog lookups under RLS.

    The caller supplies its live execution connection. No catalog result is
    cached across queries; CTE names are resolved by SQL scope analysis.
    Metadata-only system views in ``_METADATA_VIEWS`` remain readable so
    schema introspection keeps working; every other view is refused.
    """
    try:
        relations = _relations(sql, dialect)
        if not relations:
            return
        cursor = connection.cursor()
        try:
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
                        "SELECT c.relkind, n.nspname, c.relname "
                        "FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.oid = pg_catalog.to_regclass(%s)",
                        (relation.sql(dialect="postgres"),),
                    )
                    row = cursor.fetchone()
                    kind = row[0] if row else None
                    if kind == "v" and tuple(row[1:3]) in _METADATA_VIEWS["postgres"]:
                        continue
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
                if kind in ("view", "v", "m", "VIEW", "SYSTEM VIEW"):
                    raise RlsError(
                        "database views are not supported while row-level security is active"
                    )
                if kind not in ("table", "r", "p", "BASE TABLE"):
                    raise RlsError(
                        "row-level security could not verify a base table; query blocked"
                    )
        finally:
            cursor.close()
    except RlsError:
        raise
    except Exception as exc:
        raise RlsError("row-level security could not verify relation types; query blocked") from exc


def _sqlite_kind(cursor: Any, relation: exp.Table) -> str | None:
    if relation.db:
        schemas = [relation.db]
    else:
        cursor.execute("PRAGMA database_list")
        schemas = ["temp", "main"] + [row[1] for row in cursor.fetchall() if row[1] != "main"]
    for schema in schemas:
        if relation.name.lower() in (
            "sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"
        ):
            return "table"
        cursor.execute(
            "SELECT type FROM pragma_table_list(?) WHERE schema = ? COLLATE NOCASE",
            (relation.name, schema),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    return None
