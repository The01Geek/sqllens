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
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_ITEMS,
    QUESTION_MAX,
    SQL_MAX,
    MemoryBundle,
    SchemaDoc,
    SqlPair,
    SqlPairsBlock,
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


def test_parse_json_rejects_oversize_bundle() -> None:
    # A bundle larger than MAX_BUNDLE_BYTES is refused before parse so the
    # server cannot be DoS'd into allocating the parsed object graph.
    payload = "x" * (MAX_BUNDLE_BYTES + 1)
    with pytest.raises(BundleFormatError, match="exceeds"):
        parse_json(payload)


def test_parse_csv_rejects_oversize_bundle() -> None:
    payload = "x" * (MAX_BUNDLE_BYTES + 1)
    with pytest.raises(BundleFormatError, match="exceeds"):
        parse_csv(payload)


def test_sql_pairs_block_rejects_too_many_pairs() -> None:
    # Per-block item cap defends against a structurally-valid JSON whose pairs
    # list, alone, is large enough to dominate the import_lock window.
    too_many = [{"question": "q", "sql": "SELECT 1"}] * (MAX_BUNDLE_ITEMS + 1)
    with pytest.raises(ValidationError):
        SqlPairsBlock(pairs=too_many)


def test_schema_docs_rejects_too_many_entries() -> None:
    too_many = [{"content": "c"}] * (MAX_BUNDLE_ITEMS + 1)
    with pytest.raises(ValidationError):
        MemoryBundle(schema_docs=too_many)


def test_parse_csv_defangs_formula_triggers() -> None:
    # CSV-injection (CWE-1236): a cell starting with =/+/-/@/\t/\r becomes a
    # formula trigger in Excel/LibreOffice. parse_csv must prefix with ' so
    # the stored value can never detonate downstream.
    text = (
        "question,sql\n"
        '=cmd|"/c calc"!A1,SELECT 1\n'
        '+1+1,SELECT 2\n'
        '-5,SELECT 3\n'
        '@foo,SELECT 4\n'
    )
    bundle = parse_csv(text)
    assert bundle.sql_pairs is not None
    pairs = bundle.sql_pairs.pairs
    assert pairs[0].question.startswith("'=")
    assert pairs[1].question.startswith("'+")
    assert pairs[2].question.startswith("'-")
    assert pairs[3].question.startswith("'@")


def test_serialize_csv_defangs_formula_triggers() -> None:
    # Symmetric guard: a poisoned in-store value cannot escape via export.
    bundle = MemoryBundle.model_validate(
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "=DANGER()", "sql": "SELECT 1"},
                    {"question": "ok", "sql": "@evil"},
                ]
            }
        }
    )
    text = serialize_csv(bundle)
    # csv writer quotes any field containing the field-separator or quotes; the
    # leading apostrophe ends up either bare or inside the quoted field.
    assert "'=DANGER()" in text
    assert "'@evil" in text


def test_csv_defang_is_idempotent() -> None:
    # An already-defanged cell (leading apostrophe) must not accumulate
    # apostrophes when re-imported then re-exported.
    text = "question,sql\n'=safe,SELECT 1\n"
    bundle = parse_csv(text)
    assert bundle.sql_pairs is not None
    out = serialize_csv(bundle)
    # Exactly one leading apostrophe survives in the re-emitted cell.
    assert "''=safe" not in out
    assert "'=safe" in out
