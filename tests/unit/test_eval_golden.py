# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :mod:`sqllens.eval.golden`.

The load-bearing claim in ``load_golden``'s docstring is that the golden file
inherits every safety cap from the memory-bundle parser
(``MAX_BUNDLE_BYTES``, ``MAX_BUNDLE_ITEMS``, JSON recursion-bomb guard,
CSV-injection defang). These tests pin that inheritance against the actual
parser so a future refactor that bypasses :mod:`sqllens.memory.io` cannot
silently drop the safety surface.
"""

from __future__ import annotations

import json

import pytest

from sqllens.eval.golden import GoldenCase, load_golden
from sqllens.memory.io import BundleFormatError
from sqllens.memory.schema import MAX_BUNDLE_BYTES, MAX_BUNDLE_ITEMS


def test_load_golden_json_happy_path() -> None:
    text = json.dumps(
        {
            "sql_pairs": {
                "pairs": [
                    {"question": "q1", "sql": "SELECT 1"},
                    {"question": "q2", "sql": "SELECT 2"},
                ]
            }
        }
    )
    cases = load_golden(text, "json")
    assert cases == [
        GoldenCase(question="q1", expected_sql="SELECT 1"),
        GoldenCase(question="q2", expected_sql="SELECT 2"),
    ]


def test_load_golden_csv_happy_path() -> None:
    text = "question,sql\nq1,SELECT 1\nq2,SELECT 2\n"
    cases = load_golden(text, "csv")
    assert cases == [
        GoldenCase(question="q1", expected_sql="SELECT 1"),
        GoldenCase(question="q2", expected_sql="SELECT 2"),
    ]


def test_load_golden_empty_bundle_returns_empty_list() -> None:
    """An empty bundle parses cleanly but yields zero cases.

    The CLI then surfaces this as a "nothing to verify" error — see the
    matching CLI test. The loader itself does not raise.
    """
    assert load_golden("{}", "json") == []


def test_load_golden_schema_docs_only_returns_empty_list() -> None:
    """schema_docs are ignored; only sql_pairs are ground-truth cases."""
    text = json.dumps({"schema_docs": [{"content": "users table"}]})
    assert load_golden(text, "json") == []


def test_load_golden_invalid_fmt_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown golden-file format"):
        load_golden("{}", "xml")


def test_load_golden_malformed_json_raises_bundle_format_error() -> None:
    with pytest.raises(BundleFormatError):
        load_golden("not valid json {", "json")


def test_load_golden_inherits_size_cap() -> None:
    """A bundle larger than ``MAX_BUNDLE_BYTES`` must be rejected at parse.

    Without inheritance, a multi-MB golden file would allocate the full
    object graph before any validation — the cap is a DoS guard.
    """
    payload = "x" * (MAX_BUNDLE_BYTES + 10)
    with pytest.raises(BundleFormatError, match="cap"):
        load_golden(payload, "json")


def test_load_golden_inherits_recursion_guard() -> None:
    """A deeply-nested JSON payload must be rejected, not blow the stack."""
    payload = "[" * 5000 + "]" * 5000
    with pytest.raises(BundleFormatError):
        load_golden(payload, "json")


def test_load_golden_inherits_item_cap() -> None:
    """More than ``MAX_BUNDLE_ITEMS`` pairs must be rejected at parse."""
    pairs = [
        {"question": f"q{i}", "sql": f"SELECT {i}"}
        for i in range(MAX_BUNDLE_ITEMS + 1)
    ]
    text = json.dumps({"sql_pairs": {"pairs": pairs}})
    with pytest.raises(BundleFormatError, match="cap"):
        load_golden(text, "json")


def test_load_golden_csv_defang_applied_to_sql_cell() -> None:
    """A CSV cell beginning with a formula trigger is defanged with a leading
    apostrophe — exact same behaviour as ``import-memory`` so a planted file
    cannot detonate when later opened in a spreadsheet.

    Verifies the defang is *visible* in the loaded case (the apostrophe
    survives), so an operator authoring SQL with leading ``-``/``=``/``@`` in
    a CSV file knows the SQL was rewritten.
    """
    text = "question,sql\nq1,=SUM(A1)\n"
    cases = load_golden(text, "csv")
    assert cases == [GoldenCase(question="q1", expected_sql="'=SUM(A1)")]


def test_load_golden_csv_rejects_missing_header() -> None:
    text = "q1,SELECT 1\n"
    with pytest.raises(BundleFormatError, match="header"):
        load_golden(text, "csv")
