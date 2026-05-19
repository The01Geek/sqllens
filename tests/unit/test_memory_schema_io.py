# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Schema validation + JSON/CSV (de)serialization. No ChromaDB."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqllens.memory.io import (
    BundleFormatError,
    parse_csv,
    parse_json,
    serialize_csv,
    serialize_json,
)
from sqllens.memory.schema import (
    CONTENT_MAX,
    QUESTION_MAX,
    SQL_MAX,
    MemoryBundle,
    SchemaDoc,
    SqlPair,
)


def test_sql_pair_length_limits() -> None:
    SqlPair(question="q" * QUESTION_MAX, sql="s" * SQL_MAX)
    with pytest.raises(ValidationError):
        SqlPair(question="q" * (QUESTION_MAX + 1), sql="ok")
    with pytest.raises(ValidationError):
        SqlPair(question="ok", sql="s" * (SQL_MAX + 1))


def test_schema_doc_length_limit_and_blank() -> None:
    SchemaDoc(content="c" * CONTENT_MAX)
    with pytest.raises(ValidationError):
        SchemaDoc(content="c" * (CONTENT_MAX + 1))
    with pytest.raises(ValidationError):
        SchemaDoc(content="   ")


def test_bundle_blocks_optional() -> None:
    assert MemoryBundle().sql_pairs is None
    assert MemoryBundle().schema_docs is None


def test_parse_json_rejects_non_object() -> None:
    with pytest.raises(BundleFormatError):
        parse_json("[]")
    with pytest.raises(BundleFormatError):
        parse_json("{ not json")


def test_parse_json_rejects_unknown_keys() -> None:
    with pytest.raises(BundleFormatError):
        parse_json('{"sql_pairs": {"pairs": []}, "bogus": 1}')


def test_json_round_trip() -> None:
    src = MemoryBundle.model_validate(
        {
            "sql_pairs": {"pairs": [{"question": "How many?", "sql": "SELECT 1"}]},
            "schema_docs": [{"content": "users table"}],
        }
    )
    again = parse_json(serialize_json(src))
    assert again == src


def test_csv_well_formed() -> None:
    bundle = parse_csv("question,sql\nHow many users?,SELECT count(*) FROM users\n")
    assert bundle.sql_pairs is not None
    assert bundle.sql_pairs.pairs[0].sql == "SELECT count(*) FROM users"


def test_csv_missing_column() -> None:
    with pytest.raises(BundleFormatError, match="header"):
        parse_csv("question\nonly one column\n")


def test_csv_wrong_column_count() -> None:
    with pytest.raises(BundleFormatError, match="2 columns"):
        parse_csv("question,sql\na,b,c\n")


def test_csv_oversized_field() -> None:
    with pytest.raises(BundleFormatError):
        parse_csv(f"question,sql\n{'q' * (QUESTION_MAX + 1)},SELECT 1\n")


def test_csv_serialize_pairs_only() -> None:
    bundle = MemoryBundle.model_validate(
        {
            "sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]},
            "schema_docs": [{"content": "ignored in csv"}],
        }
    )
    text = serialize_csv(bundle)
    assert text.splitlines()[0] == "question,sql"
    assert "ignored in csv" not in text
