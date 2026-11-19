# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``inherit`` and ``referenced_by`` joins against a real MySQL 8.0+ database (issue #416).

Needs ``SQLLENS_TEST_MYSQL_URL`` (a user that can create tables). Tables carry
an ``rj_`` prefix; every join column is nullable and not a primary key. User 7
is the request's user:

- ``rj_src`` (owner 7 visible): 1 (ref 1, key 1), 2 (ref NULL, key NULL),
  3 (ref 3, key 3, owner 8: hidden), 4 (ref 1, key 4).
- ``rj_direct`` (direct ``referenced_by`` on ``tid`` from ``rj_src.ref``):
  1 (tid 1, from sources 1 and 4), 2 (tid NULL), 3 (tid 3, hidden source),
  4 (tid 5, no source), 5 (tid 1); visible 1, 5.
- ``rj_link`` (listed, own rule ``flag = 1``) and ``rj_linked`` (linked
  ``referenced_by`` on ``tid`` through ``rj_link`` from ``rj_src.key_id``):
  1 (tid 10, two identical link rows from source 1), 2 (tid 11, from sources
  1 and 4), 3 (tid 12, only link row has flag 0), 4 (tid NULL, link dst NULL),
  5 (tid 14, link src_key NULL against source 2's NULL key), 6 (tid 15, hidden
  source); visible 1, 2.
- ``rj_parent`` (owner 7 visible): 1 (pkey 1), 2 (pkey NULL), 3 (pkey 3, owner 8);
  ``rj_child`` (``inherit`` ``pref`` -> ``pkey``): 1 (pref 1), 2 (pref NULL),
  3 (pref 3); visible 1.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from sqllens.rule_sets import (
    RuleSetCatalogError,
    RuleSetSelection,
    check_catalog,
    load_rule_sets,
    reset_rule_set_selection,
    set_rule_set_selection,
)
from tests.connectors.test_row_rules_mysql import _chain, _connect, _ids, _run, _url
from tests.unit._rule_set_fixtures import example_document, to_toml, user_id_compare

pytestmark = pytest.mark.asyncio

_TABLES = "rj_src, rj_direct, rj_link, rj_linked, rj_parent, rj_child, rj_tp_child, rj_tp_parent"
_TEARDOWN = [f"DROP TABLE IF EXISTS {_TABLES}"]

_SETUP = [
    *_TEARDOWN,
    "CREATE TABLE rj_src (id INT PRIMARY KEY, ref INT NULL, key_id INT NULL, owner BIGINT)",
    "CREATE TABLE rj_direct (id INT PRIMARY KEY, tid INT NULL)",
    "CREATE TABLE rj_link (src_key INT NULL, dst INT NULL, flag INT)",
    "CREATE TABLE rj_linked (id INT PRIMARY KEY, tid INT NULL)",
    "CREATE TABLE rj_parent (id INT PRIMARY KEY, pkey INT NULL, owner BIGINT)",
    "CREATE TABLE rj_child (id INT PRIMARY KEY, pref INT NULL)",
    "INSERT INTO rj_src VALUES (1, 1, 1, 7), (2, NULL, NULL, 7), (3, 3, 3, 8), (4, 1, 4, 7)",
    "INSERT INTO rj_direct VALUES (1, 1), (2, NULL), (3, 3), (4, 5), (5, 1)",
    "INSERT INTO rj_link VALUES (1, 10, 1), (1, 10, 1), (1, 11, 1), (4, 11, 1), (1, 12, 0),"
    " (1, NULL, 1), (NULL, 14, 1), (3, 15, 1)",
    "INSERT INTO rj_linked VALUES (1, 10), (2, 11), (3, 12), (4, NULL), (5, 14), (6, 15)",
    "INSERT INTO rj_parent VALUES (1, 1, 7), (2, NULL, 7), (3, 3, 8)",
    "INSERT INTO rj_child VALUES (1, 1), (2, NULL), (3, 3)",
]

VISIBLE = {
    "rj_src": {1, 2, 4},
    "rj_direct": {1, 5},
    "rj_linked": {1, 2},
    "rj_parent": {1, 2},
    "rj_child": {1},
}


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    url = _url()
    conn = _connect(url)
    try:
        for statement in _SETUP:
            conn.cursor().execute(statement)
    finally:
        conn.close()
    try:
        yield url
    finally:
        conn = _connect(url)
        try:
            for statement in _TEARDOWN:
                conn.cursor().execute(statement)
        finally:
            conn.close()


@pytest.fixture
def selected() -> Iterator[None]:
    token = set_rule_set_selection(RuleSetSelection("2", "5", 7))
    yield
    reset_rule_set_selection(token)


def _document(database: str) -> dict[str, Any]:
    document = example_document()
    document["binding"]["database"] = database
    document["rule_sets"] = [document["rule_sets"][0]]
    document["rule_sets"][0]["tables"] = [
        {"name": "rj_src", "columns": ["id", "ref", "key_id"], "predicate_columns": ["owner"],
         "row_rule": user_id_compare("owner")},
        {"name": "rj_direct", "columns": ["id", "tid"],
         "row_rule": {"kind": "referenced_by", "id_column": "tid", "source_table": "rj_src",
                      "source_column": "ref"}},
        {"name": "rj_link", "columns": ["src_key", "dst"], "predicate_columns": ["flag"],
         "row_rule": {"kind": "compare", "column": "flag", "operator": "=",
                      "value_type": "integer", "value": 1}},
        {"name": "rj_linked", "columns": ["id", "tid"],
         "row_rule": {"kind": "referenced_by", "id_column": "tid", "source_table": "rj_src",
                      "source_id_column": "key_id",
                      "link": {"table": "rj_link", "source_column": "src_key",
                               "target_column": "dst"}}},
        {"name": "rj_parent", "columns": ["id", "pkey"], "predicate_columns": ["owner"],
         "row_rule": user_id_compare("owner")},
        {"name": "rj_child", "columns": ["id", "pref"],
         "row_rule": {"kind": "inherit", "column": "pref", "parent_table": "rj_parent",
                      "parent_column": "pkey"}},
    ]
    return document


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _loaded(tmp_path: Path, url: str, document: dict[str, Any] | None = None):
    path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
    path.write_text(to_toml(document or _document(_db_name(url))))
    return load_rule_sets(path, database_url=url)


async def test_each_table_returns_exactly_its_visible_rows(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    for table, ids in VISIBLE.items():
        assert _ids(await _run(runner, f"SELECT t.id FROM {table} t")) == ids, table


async def test_a_hidden_link_row_hides_its_target_but_not_its_source(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    assert 3 not in _ids(await _run(runner, "SELECT t.id FROM rj_linked t"))
    assert 1 in _ids(await _run(runner, "SELECT s.id FROM rj_src s"))
    links = await _run(runner, "SELECT l.src_key, l.dst FROM rj_link l WHERE l.dst = 12")
    assert links.empty


@pytest.mark.parametrize(
    ("table", "hidden"),
    [
        ("rj_child", 2),  # NULL pref against visible parent 2's NULL pkey
        ("rj_direct", 2),  # NULL tid against visible source 2's NULL ref
        ("rj_linked", 4),  # only link row's NULL dst against a NULL tid
        ("rj_linked", 5),  # only link row's NULL src_key against source 2's NULL key_id
    ],
)
async def test_a_null_key_never_matches_a_null_key(
    database: str, tmp_path: Path, selected, table: str, hidden: int
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    assert hidden not in _ids(await _run(runner, f"SELECT t.id FROM {table} t"))


async def test_a_target_reached_twice_is_returned_once(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    for table, expected in (("rj_direct", [1, 5]), ("rj_linked", [1, 2])):
        frame = await _run(runner, f"SELECT t.id FROM {table} t ORDER BY t.id")
        assert [int(v) for v in frame.iloc[:, 0].tolist()] == expected, table
        count = await _run(runner, f"SELECT COUNT(*) FROM {table}")
        assert int(count.iloc[0, 0]) == len(expected), table


_SHAPES = [
    "SELECT x.id FROM {t} x",
    "SELECT a.id FROM {t} a JOIN {t} b ON b.id = a.id",
    "SELECT id FROM {t} WHERE id = 99 UNION SELECT id FROM {t}",
    "WITH c AS (SELECT x.id FROM {t} x) SELECT c.id FROM c",
    "SELECT d.id FROM rj_src s JOIN LATERAL (SELECT x.id FROM {t} x WHERE x.id <> s.id + 100) d"
    " ON TRUE",
]


@pytest.mark.parametrize("table", ["rj_direct", "rj_linked"])
@pytest.mark.parametrize("shape", _SHAPES, ids=["alias", "self-join", "union", "cte", "lateral"])
async def test_each_query_shape_returns_only_visible_rows(
    database: str, tmp_path: Path, selected, table: str, shape: str
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    assert _ids(await _run(runner, shape.format(t=table))) == VISIBLE[table]


@pytest.mark.parametrize("table", ["rj_direct", "rj_linked"])
async def test_a_scalar_subquery_counts_only_visible_rows(
    database: str, tmp_path: Path, selected, table: str
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    frame = await _run(runner, f"SELECT (SELECT COUNT(*) FROM {table} x) AS n")
    assert _ids(frame) == {len(VISIBLE[table])}


# --- join-pair types ----------------------------------------------------------------


def _typed_document(database: str) -> dict[str, Any]:
    document = example_document()
    document["binding"]["database"] = database
    document["rule_sets"] = [document["rule_sets"][0]]
    document["rule_sets"][0]["tables"] = [
        {"name": "rj_tp_parent", "columns": ["id", "k"]},
        {"name": "rj_tp_child", "columns": ["id", "k"],
         "row_rule": {"kind": "inherit", "column": "k", "parent_table": "rj_tp_parent",
                      "parent_column": "k"}},
    ]
    return document


@pytest.mark.parametrize(
    ("child", "parent", "refused"),
    [
        ("INT", "BIGINT", None),
        ("CHAR(5)", "VARCHAR(5)", None),
        ("DECIMAL(10,2)", "DECIMAL(10,2)", None),
        ("VARCHAR(10)", "INT", ("varchar", "int")),
        ("DECIMAL(10,2)", "BIGINT", ("decimal", "bigint")),
    ],
)
async def test_the_catalog_check_types_each_join_pair(
    database: str, tmp_path: Path, child: str, parent: str, refused: tuple[str, str] | None
) -> None:
    conn = _connect(database)
    try:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS rj_tp_child, rj_tp_parent")
        cursor.execute(f"CREATE TABLE rj_tp_parent (id INT PRIMARY KEY, k {parent})")
        cursor.execute(f"CREATE TABLE rj_tp_child (id INT PRIMARY KEY, k {child})")
        loaded = _loaded(tmp_path, database, _typed_document(_db_name(database)))
        if refused is None:
            check_catalog(conn, loaded)
            return
        with pytest.raises(RuleSetCatalogError) as info:
            check_catalog(conn, loaded)
        assert (
            f"join columns rj_tp_child.k (type {refused[0]!r}) and rj_tp_parent.k "
            f"(type {refused[1]!r}) have types a row rule cannot join"
        ) in str(info.value)
    finally:
        conn.cursor().execute("DROP TABLE IF EXISTS rj_tp_child, rj_tp_parent")
        conn.close()
