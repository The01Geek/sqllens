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
