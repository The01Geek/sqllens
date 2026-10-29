# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Verify RLS query relations using the connection that will execute them."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import traverse_scope

from sqllens.safety.rls import RlsError


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


def verify_base_tables(connection: Any, sql: str, *, dialect: str) -> None:
    """Refuse views, unknown relation kinds, and failed catalog lookups under RLS.

    The caller supplies its live execution connection. No catalog result is
    cached across queries; CTE names are resolved by SQL scope analysis.
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
                    cursor.execute(
                        "SELECT relkind FROM pg_catalog.pg_class "
                        "WHERE oid = pg_catalog.to_regclass(%s)",
                        (relation.sql(dialect="postgres"),),
                    )
                    row = cursor.fetchone()
                    kind = row[0] if row else None
                elif dialect == "mysql":
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
        if relation.name.lower() in ("sqlite_master", "sqlite_schema"):
            return "table"
        quoted = exp.to_identifier(schema, quoted=True).sql(dialect="sqlite")
        cursor.execute(
            f"SELECT type FROM {quoted}.sqlite_master WHERE name = ? COLLATE NOCASE",
            (relation.name,),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    return None
