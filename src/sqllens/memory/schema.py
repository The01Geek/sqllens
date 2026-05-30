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
# authenticated client could still DoS the server by submitting millions of
# valid-but-cheap items inside one bundle (parsing the list, then writing each
# inside the held ``import_lock``). The two caps below — both enforced in
# ``memory.io`` at the parse boundary, *not* as model-level ``Field``
# constraints — reject such payloads on the way in while leaving in-process
# constructors (notably ``MemoryStore.iter_all``, which is the dedup baseline
# for ``import_bundle`` and the source for ``export_bundle``) unrestricted.
# Enforcing as a ``Field`` constraint would propagate the cap to every
# construction, breaking export and import-baseline reads on a healthy store
# that legitimately holds more than ``MAX_BUNDLE_ITEMS`` rows.
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


class SqlPairsBlock(BaseModel):
    """The ``sql_pairs`` top-level block."""

    model_config = ConfigDict(extra="forbid")

    training_type: Literal["sql_pairs"] = "sql_pairs"
    pairs: list[SqlPair] = Field(default_factory=list)


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
    """The full importable/exportable bundle. Both blocks are optional."""

    model_config = ConfigDict(extra="forbid")

    sql_pairs: SqlPairsBlock | None = None
    schema_docs: list[SchemaDoc] | None = None


class ImportItemError(BaseModel):
    """A single rejected item, surfaced in the report rather than aborting."""

    kind: Literal["sql_pair", "schema_doc"]
    index: int
    message: str


class ImportReport(BaseModel):
    """Outcome of an import run."""

    saved: int = 0
    skipped_duplicate: int = 0
    errors: list[ImportItemError] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Render as a compact Markdown summary (used by the MCP tool)."""
        lines = [
            "| metric | count |",
            "| --- | --- |",
            f"| saved | {self.saved} |",
            f"| skipped (duplicate) | {self.skipped_duplicate} |",
            f"| errors | {len(self.errors)} |",
        ]
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"- `{err.kind}[{err.index}]`: {err.message}")
        return "\n".join(lines)
