# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Golden-file loader for ``sqllens verify-memory``.

Reuses the existing memory-bundle parser (:mod:`sqllens.memory.io`) so no new
file format is introduced. The on-disk shape is identical to an
``import-memory`` bundle's ``sql_pairs`` block — but the semantic differs:

- In a memory bundle, each ``(question, sql)`` pair is a *training hint to
  store* in vector memory.
- In a golden file, each ``(question, sql)`` pair is the *expected agent
  output to match against* (the ``sql`` is ground truth, not a hint).

By reusing the parser we inherit every existing safety cap for free:
``MAX_BUNDLE_BYTES``, ``MAX_BUNDLE_ITEMS``, the JSON recursion-bomb guard,
and the CSV-injection defang.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenCase:
    """A single curated ``question -> expected SQL`` ground-truth pair."""

    question: str
    expected_sql: str


def load_golden(text: str, fmt: str) -> list[GoldenCase]:
    """Parse a golden-file ``text`` (JSON or CSV) into a list of cases.

    ``fmt`` must be one of :data:`sqllens.memory.io.VALID_FORMATS`. The
    bundle's ``schema_docs`` block (if any) is ignored — only ``sql_pairs``
    are ground-truth cases. A bundle with no ``sql_pairs`` block, or with an
    empty ``pairs`` list, returns ``[]``; the CLI surfaces that as an explicit
    "nothing to verify" error per the CLAUDE.md "structured signal, never
    silent success" contract.

    Raises :class:`sqllens.memory.io.BundleFormatError` on a malformed bundle
    (size cap, schema violation, etc.) — the parser's existing error path.
    """
    from sqllens.memory.io import VALID_FORMATS, parse_csv, parse_json

    if fmt not in VALID_FORMATS:
        raise ValueError(
            f"unknown golden-file format {fmt!r}; expected one of {VALID_FORMATS}"
        )
    bundle = parse_csv(text) if fmt == "csv" else parse_json(text)
    if bundle.sql_pairs is None:
        return []
    return [
        GoldenCase(question=pair.question, expected_sql=pair.sql)
        for pair in bundle.sql_pairs.pairs
    ]
