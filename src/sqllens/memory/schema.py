# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the memory bundle file format.

The bundle is the on-disk interchange format for ``import-memory`` /
``export-memory``. JSON is canonical and round-trips losslessly; CSV is a
convenience for SQL pairs only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

QUESTION_MAX = 1000
SQL_MAX = 10000
CONTENT_MAX = 50000

# Defence-in-depth caps on the *outer* shape of a bundle. ``QUESTION_MAX`` /
# ``SQL_MAX`` / ``CONTENT_MAX`` bound the size of any single item; an
# authenticated client could still DoS the MCP ``import_memory`` tool by
# submitting millions of valid-but-cheap items inside one bundle (parsing the
# list, then writing each inside the held ``import_lock``). The two caps below
# are enforced in ``memory.io`` at the parse boundary (not as model-level
# ``Field`` constraints) and apply to **the MCP path and the bounded CLI
# default**. The CLI ``--stream`` path bypasses these whole-file caps
# (memory-bounded streaming makes the DoS lever irrelevant for a local
# operator) but keeps the per-item caps row by row. Leaving these as parse-
# boundary checks rather than ``Field`` constraints means in-process
# constructors (notably ``MemoryStore.iter_all`` and
# ``MemoryStore.iter_paginated``, which back ``export_bundle`` /
# ``export_bundle_stream``) stay unrestricted: enforcing as a ``Field``
# constraint would propagate the cap to every construction, breaking exports
# on a healthy store that legitimately holds more than ``MAX_BUNDLE_ITEMS``
# rows.
MAX_BUNDLE_BYTES = 10 * 1024 * 1024
"""Hard ceiling on the raw bundle text accepted by ``parse_json``/``parse_csv``.

Realistic curated bundles fit well under 10 MiB; anything larger is treated
as a DoS payload and refused before allocation of the parsed object graph.
Measured against the UTF-8-encoded byte length of the input (not the
character count) so a multi-byte payload cannot bypass the cap by up to 4x."""

MAX_BUNDLE_ITEMS = 10_000
"""Per-block item cap enforced by ``memory.io`` after parse. Sized to cover
the largest realistic curated bundles while still bounding the work done
under ``import_lock``."""


def _require_non_blank(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    return value


class SqlPair(BaseModel):
    """A single curated question→SQL training pair."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=QUESTION_MAX)
    sql: str = Field(max_length=SQL_MAX)

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        return _require_non_blank(v, "question")

    @field_validator("sql")
    @classmethod
    def _s(cls, v: str) -> str:
        return _require_non_blank(v, "sql")


class SchemaDoc(BaseModel):
    """A single free-form schema / documentation memory."""

    model_config = ConfigDict(extra="forbid")

    training_type: Literal["schema_docs"] = "schema_docs"
    content: str = Field(max_length=CONTENT_MAX)

    @field_validator("content")
    @classmethod
    def _c(cls, v: str) -> str:
        return _require_non_blank(v, "content")


class MemoryBundle(BaseModel):
    """The full importable/exportable bundle. Both blocks are optional.

    ``sql_pairs`` is a flat array of ``{question, sql}`` objects — the same
    shape the memory-admin ``add_memories`` tool accepts — so a bundle written
    by ``export-memory`` round-trips through every import surface (CLI
    ``import-memory``, the MCP ``import_memory`` tool, and the widget).
    """

    model_config = ConfigDict(extra="forbid")

    sql_pairs: list[SqlPair] | None = None
    schema_docs: list[SchemaDoc] | None = None


class ImportItemError(BaseModel):
    """A single rejected item, surfaced in the report rather than aborting."""

    kind: Literal["sql_pair", "schema_doc"]
    index: int
    message: str


class ImportReport(BaseModel):
    """Outcome of an import run.

    ``saved`` counts items successfully upserted into the store. Because
    import now writes rows under a content-hash id (see
    :func:`sqllens.memory.store.sql_pair_id` /
    :func:`sqllens.memory.store.schema_doc_id`) and ``collection.upsert``
    is idempotent, a re-imported logical pair overwrites the same row
    rather than reporting a "skipped duplicate" — there is no separate
    skip count to surface. ``errors`` holds per-item validation /
    save failures; the per-item-error count is what callers compare
    against zero to decide whether an import succeeded.
    """

    saved: int = 0
    errors: list[ImportItemError] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Render as a compact Markdown summary (used by the MCP tool)."""
        lines = [
            "| metric | count |",
            "| --- | --- |",
            f"| saved | {self.saved} |",
            f"| errors | {len(self.errors)} |",
        ]
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"- `{err.kind}[{err.index}]`: {err.message}")
        return "\n".join(lines)
