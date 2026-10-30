# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Schema validation + JSON/CSV (de)serialization. No ChromaDB."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sqllens.memory import io as memory_io
from sqllens.memory.io import (
    MAX_BUNDLE_DEPTH,
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


@pytest.mark.parametrize("separator", ["\r", "\n", "\r\n"])
@pytest.mark.parametrize("defang", [False, True])
def test_csv_record_separators_preserve_quoted_newlines(separator: str, defang: bool) -> None:
    text = f'question,sql{separator}"first\r\nsecond","SELECT \'a\r\nb\'"{separator}'
    bundle = parse_csv(text, defang=defang)
    pair = bundle.sql_pairs.pairs[0]
    assert pair.question == "first\r\nsecond"
    assert pair.sql == "SELECT 'a\r\nb'"


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


def test_parse_json_rejects_deeply_nested_under_size_cap() -> None:
    # Regression: on Python 3.14 json.loads parses this without RecursionError,
    # so only the explicit depth limit makes the message "deeply nested".
    depth = 10_000
    payload = "[" * depth + "1" + "]" * depth
    assert len(payload.encode("utf-8")) < MAX_BUNDLE_BYTES
    with pytest.raises(BundleFormatError, match="deeply nested"):
        parse_json(payload)


def _nested_arrays(depth: int) -> str:
    return "[" * depth + "1" + "]" * depth


def _object_over_arrays(depth: int) -> str:
    return '{"x": ' + _nested_arrays(depth - 1) + "}"


def _nested_objects(depth: int) -> str:
    return '{"a": ' * depth + "1" + "}" * depth


_NESTED_SHAPES = pytest.mark.parametrize(
    "build", [_nested_arrays, _object_over_arrays, _nested_objects]
)


@_NESTED_SHAPES
@pytest.mark.parametrize("depth", [MAX_BUNDLE_DEPTH + 1, 10_000])
def test_parse_json_rejects_nesting_beyond_depth_limit(build, depth: int) -> None:
    with pytest.raises(BundleFormatError, match="deeply nested"):
        parse_json(build(depth))


@_NESTED_SHAPES
def test_parse_json_depth_limit_admits_max_depth(build) -> None:
    # Depth 64 passes the depth check and fails later on the schema or root
    # type, so the message must not be the depth one.
    with pytest.raises(BundleFormatError) as excinfo:
        parse_json(build(MAX_BUNDLE_DEPTH))
    assert "deeply nested" not in str(excinfo.value)


def test_parse_json_depth_walk_reaches_later_siblings() -> None:
    # The deep branch sits in the second list item and the second key, so a
    # walk that follows only the first child would miss it.
    payload = (
        '{"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}, '
        '"schema_docs": [{"content": "c"}, ' + _nested_arrays(MAX_BUNDLE_DEPTH) + "]}"
    )
    with pytest.raises(BundleFormatError, match="deeply nested"):
        parse_json(payload)


def test_parse_json_maps_loads_recursion_error_to_deeply_nested(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(memory_io.json, "loads", _raise)
    with pytest.raises(BundleFormatError, match="deeply nested") as excinfo:
        parse_json('{"schema_docs": []}')
    assert isinstance(excinfo.value.__cause__, RecursionError)


def test_parse_json_rejects_multibyte_oversize_below_codepoint_cap() -> None:
    # Pins the docstring's promise that the cap is measured against UTF-8
    # bytes, not code points. The pad is "🚀" (4 UTF-8 bytes per code point):
    # code-point count is well under MAX_BUNDLE_BYTES but byte count is over,
    # so a regression that swaps len(text.encode("utf-8")) for len(text)
    # would silently 4x-inflate the allowed payload size.
    pad_chars = (MAX_BUNDLE_BYTES // 4) + 1
    payload = "🚀" * pad_chars
    assert len(payload) <= MAX_BUNDLE_BYTES
    assert len(payload.encode("utf-8")) > MAX_BUNDLE_BYTES
    with pytest.raises(BundleFormatError, match="exceeds"):
        parse_json(payload)


def test_parse_json_rejects_too_many_pairs() -> None:
    # Per-block item cap defends against a structurally-valid JSON whose pairs
    # list, alone, is large enough to dominate the import_lock window.
    too_many = [{"question": "q", "sql": "SELECT 1"}] * (MAX_BUNDLE_ITEMS + 1)
    payload = '{"sql_pairs": {"pairs": ' + json.dumps(too_many) + "}}"
    with pytest.raises(BundleFormatError, match=r"sql_pairs\.pairs.*exceeds"):
        parse_json(payload)


def test_parse_json_rejects_too_many_schema_docs() -> None:
    too_many = [{"content": "c"}] * (MAX_BUNDLE_ITEMS + 1)
    payload = '{"schema_docs": ' + json.dumps(too_many) + "}"
    with pytest.raises(BundleFormatError, match=r"schema_docs.*exceeds"):
        parse_json(payload)


def test_models_accept_more_than_bundle_item_cap_via_construction() -> None:
    # The cap fires only at the parse boundary. ``MemoryStore.iter_all``
    # constructs the same models from a live store that may legitimately
    # exceed the cap; a model-level constraint would crash export/import on
    # healthy data with > MAX_BUNDLE_ITEMS rows.
    overflow = [SqlPair(question=f"q{i}", sql="SELECT 1") for i in range(3)]
    pairs = overflow * (MAX_BUNDLE_ITEMS // len(overflow) + 1)
    block = SqlPairsBlock(pairs=pairs)
    assert len(block.pairs) > MAX_BUNDLE_ITEMS


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


def test_csv_defang_preserves_blank_rejection_semantics() -> None:
    # A "\t"-only cell must STILL be rejected — defang must not turn it into
    # "'\t" (whose strip() is "'", non-blank) and silently accept a broken row.
    text = 'question,sql\n"\t","SELECT 1"\n'
    with pytest.raises(BundleFormatError):
        parse_csv(text)


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
