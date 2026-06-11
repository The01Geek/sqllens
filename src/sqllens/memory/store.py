# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Thin adapter over the vendored ``ChromaAgentMemory``.

This module is the SINGLE place that encodes two verified facts about the
vendored agent memory engine, so the rest of the package never reaches into
``agent/`` directly:

1. ``ChromaAgentMemory.save_tool_usage`` / ``save_text_memory`` /
   ``get_recent_*`` accept a ``context: ToolContext`` argument but **never
   reference it** in their inner closures (verified in
   ``agent/integrations/chromadb/agent_memory.py``). Memory can therefore be
   driven outside a live agent run with a minimal stub ``ToolContext``.

2. Imported SQL pairs MUST be stored with the exact shape the agent writes at
   query time so retrieval matches them: ``save_tool_usage`` with
   ``tool_name="run_sql"`` (the default name of ``RunSqlTool``) and
   ``args={"sql": ...}``. ``RUN_SQL_TOOL_NAME`` is asserted against the live
   tool in the test-suite so a future rename can't silently break retrieval.

Enumeration and ``clear`` use the synchronous private ``_get_collection()``
seam directly: the vendored class has no public "give me every memory"
enumerator (only ``get_recent_*`` with a limit). A public ``clear_memories``
exists but is ``async``, ``ToolContext``-bound, and deletes row-by-row; the
synchronous bulk ``collection.delete(ids=...)`` here is simpler for a full
wipe. Both fallbacks are deliberately isolated to this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.memory.schema import MemoryBundle, SchemaDoc, SqlPair

logger = logging.getLogger("sqllens.memory")

if TYPE_CHECKING:
    from sqllens.config import Config

RUN_SQL_TOOL_NAME = "run_sql"
_IMPORT_SOURCE = "import"


def _norm(text: str) -> str:
    """Normalize text for content-hash ID derivation.

    Strip leading/trailing whitespace, collapse internal whitespace, and
    lowercase. Two near-identical inputs (different casing, trailing
    whitespace, or repeated spaces) normalize to the same string and
    therefore the same content-hash ID — re-importing such inputs upserts
    the same row instead of duplicating it.
    """
    return " ".join(text.split()).lower()


def sql_pair_id(question: str, sql: str) -> str:
    """Deterministic Chroma row id for a ``(question, sql)`` import.

    ``sha256("sql_pair" + "\\x00" + _norm(question) + "\\x00" + _norm(sql))``.
    A re-import of the same logical pair targets the same id, so
    ``collection.upsert`` overwrites rather than duplicates.
    """
    parts = b"sql_pair\x00" + _norm(question).encode("utf-8") + b"\x00" + _norm(sql).encode("utf-8")
    return hashlib.sha256(parts).hexdigest()


def schema_doc_id(content: str) -> str:
    """Deterministic Chroma row id for a schema-doc import.

    ``sha256("schema_doc" + "\\x00" + _norm(content))``. Mirrors
    :func:`sql_pair_id` so a re-imported text memory upserts the same row.
    """
    parts = b"schema_doc\x00" + _norm(content).encode("utf-8")
    return hashlib.sha256(parts).hexdigest()

# Wholesale-failure guard for ``iter_all``. One bad row is a tolerated skip;
# *every* row failing to reconstruct (e.g. a chromadb/schema version skew that
# makes every ``args_json`` unparseable) is systemic corruption, not noise.
# Skipping it silently would (a) report an empty/partial export as success and
# (b) hand ``import_bundle`` an empty dedup baseline that re-saves duplicates.
# Fail loud instead, but only once enough rows exist that a high skip ratio is
# meaningful (a 1-row collection with 1 skip is not "wholesale corruption").
_WHOLESALE_MIN_ROWS = 5
_WHOLESALE_SKIP_RATIO = 0.9


class MemoryCorruptionError(RuntimeError):
    """Raised when ``iter_all`` cannot reconstruct (almost) any stored row.

    Signals systemic corruption / version skew rather than a single bad row,
    so callers fail loud instead of treating a near-empty result as success.
    """


@dataclass(frozen=True)
class MemoryRecord:
    """One raw stored row: the Chroma document id, its metadata, and (optionally)
    its embedding vector.

    This is the low-level shape the admin-tool layer (``memory.admin``) reshapes
    into the wire contract; it deliberately carries metadata verbatim so the
    discriminator (``is_text_memory``), tracking keys (``hit_count`` /
    ``last_hit_date``) and provenance are all visible to the caller.
    """

    memory_id: str
    metadata: dict[str, Any]
    embedding: list[float] | None = None


class MemoryStore:
    """Construct ``ChromaAgentMemory`` exactly as the agent factory does."""

    def __init__(self, cfg: Config) -> None:
        self._mem = ChromaAgentMemory(
            persist_directory=str(cfg.memory.persist_dir),
            collection_name=cfg.memory.collection,
        )
        # context is ignored by every method we call (see module docstring);
        # agent_memory just needs to be an AgentMemory instance — the engine
        # itself satisfies that.
        self._ctx = ToolContext(
            user=User(id="sqllens-import"),
            conversation_id="import",
            request_id="import",
            agent_memory=self._mem,
        )
        # Number of rows the most recent ``iter_all`` skipped; lets export /
        # import surface a non-fatal partial-loss warning.
        self.last_skipped_rows = 0

    async def add_sql_pair(self, question: str, sql: str) -> None:
        """Upsert a single SQL pair using a deterministic content-hash id.

        Bypasses the vendored ``save_tool_usage`` (which assigns a fresh
        ``uuid4`` per write and so duplicates rows on re-import). The metadata
        shape is byte-identical to ``save_tool_usage``'s writes so retrieval
        at query time still matches imported pairs.
        """
        await self.add_sql_pair_batch([(question, sql)])

    async def add_schema_doc(self, content: str) -> None:
        """Upsert a single schema doc using a deterministic content-hash id.

        Bypasses ``save_text_memory`` for the same reason as
        :meth:`add_sql_pair`. Metadata stays byte-identical to live-agent
        writes so retrieval still matches.
        """
        await self.add_schema_doc_batch([content])

    async def add_sql_pair_batch(self, items: list[tuple[str, str]]) -> None:
        """Batch-upsert ``(question, sql)`` pairs with content-hash ids.

        One ``collection.upsert`` per call writes the whole batch — far
        cheaper than per-item round-trips on large imports. Ids are derived
        via :func:`sql_pair_id`, so re-importing the same logical pairs is
        idempotent.

        Metadata is byte-identical to ``save_tool_usage``'s writes:

        - ``tool_name`` = ``"run_sql"`` so the agent's retrieval filter
          (``where={"tool_name": "run_sql"}``) matches the row.
        - ``args_json`` = ``{"sql": ...}`` so the SQL is reconstructable.
        - ``metadata_json`` = ``{"source": "import"}`` so curated imports are
          distinguishable from live-agent writes.
        """
        if not items:
            return
        # Collapse intra-batch id collisions before upserting. Two records
        # whose normalized content hashes to the same ``sql_pair_id`` would
        # otherwise reach ``collection.upsert`` as ``ids=[dup, dup, ...]``,
        # which ChromaDB rejects with ``DuplicateIDError`` — failing the WHOLE
        # batch (every row reported as errored, and under ``--clear`` leaving
        # the store wiped-and-empty). Keying on the content-hash id de-dupes
        # within the batch (last write wins), which is exactly the idempotent
        # overwrite a re-import already gets across batches. ``report.saved``
        # still counts records processed, not distinct rows — consistent with
        # the bounded path's per-item counting.
        timestamp = datetime.now().isoformat()
        by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        for question, sql in items:
            by_id[sql_pair_id(question, sql)] = (
                question,
                {
                    "question": question,
                    "tool_name": RUN_SQL_TOOL_NAME,
                    "args_json": json.dumps({"sql": sql}),
                    "timestamp": timestamp,
                    "success": True,
                    "metadata_json": json.dumps({"source": _IMPORT_SOURCE}),
                },
            )
        ids = list(by_id.keys())
        questions = [doc for doc, _ in by_id.values()]
        metadatas = [meta for _, meta in by_id.values()]

        def _save() -> None:
            collection = self._mem._get_collection()
            collection.upsert(ids=ids, documents=questions, metadatas=metadatas)

        await asyncio.get_event_loop().run_in_executor(self._mem._executor, _save)

    async def add_schema_doc_batch(self, contents: list[str]) -> None:
        """Batch-upsert schema docs with content-hash ids.

        Mirrors :meth:`add_sql_pair_batch` with the text-memory metadata
        shape ``{"content": ..., "timestamp": ..., "is_text_memory": True}``
        — byte-identical to ``save_text_memory``.
        """
        if not contents:
            return
        # Collapse intra-batch id collisions before upserting — see
        # ``add_sql_pair_batch`` for the full rationale. Two schema docs whose
        # normalized content hashes to the same ``schema_doc_id`` must not
        # reach ``collection.upsert`` as duplicate ids (DuplicateIDError fails
        # the whole batch); key on the content-hash id so the last one wins.
        timestamp = datetime.now().isoformat()
        by_id: dict[str, dict[str, Any]] = {}
        for content in contents:
            by_id[schema_doc_id(content)] = {
                "content": content,
                "timestamp": timestamp,
                "is_text_memory": True,
            }
        ids = list(by_id.keys())
        documents = [meta["content"] for meta in by_id.values()]
        metadatas = list(by_id.values())

        def _save() -> None:
            collection = self._mem._get_collection()
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

        await asyncio.get_event_loop().run_in_executor(self._mem._executor, _save)

    def iter_all(self) -> MemoryBundle:
        """Enumerate the collection into a bundle (bounded, single-shot).

        Only the two kinds this package can represent are exported: ``run_sql``
        tool memories carrying a ``sql`` arg (→ SQL pairs) and text memories
        (→ schema docs). Any other tool memory the live agent may have written
        is not representable in the bundle format and is skipped.

        A single corrupt or non-conforming row (unparseable ``args_json``, or a
        value the bundle models reject — e.g. a live-agent memory longer than
        the import limits) is skipped, not fatal — ``iter_all`` is the
        source for the bounded ``export_bundle`` and must not abort the
        whole export on one bad row.

        Wholesale failure is *not* tolerated: if the collection has a
        meaningful number of rows and (almost) none reconstruct, that is
        systemic corruption / version skew, and a silent empty result would
        report a destroyed backup as success. In that case
        :class:`MemoryCorruptionError` is raised. The streaming counterpart
        (:meth:`iter_paginated`) does not enforce this rule — the skip-
        ratio is not a single-snapshot statistic across pages.

        ``last_skipped_rows`` records how many rows the most recent call
        skipped so callers (export) can surface a non-fatal partial-loss
        warning.
        """
        collection = self._mem._get_collection()
        # Skip embedding vectors/documents (largest per-row payload, unused here).
        metadatas = collection.get(include=["metadatas"]).get("metadatas") or []

        pairs: list[SqlPair] = []
        docs: list[SchemaDoc] = []
        skipped = 0
        total = 0
        for metadata in metadatas:
            # Chroma can return a row with no metadata (None) — skip explicitly
            # rather than letting AttributeError abort the whole enumeration.
            if not isinstance(metadata, dict):
                skipped += 1
                total += 1
                continue
            total += 1
            try:
                if metadata.get("is_text_memory"):
                    docs.append(SchemaDoc(content=metadata.get("content", "")))
                    continue
                if metadata.get("tool_name") != RUN_SQL_TOOL_NAME:
                    continue
                args = json.loads(metadata.get("args_json", "{}"))
                sql = args.get("sql")
                if not sql:
                    continue
                pairs.append(SqlPair(question=metadata.get("question", ""), sql=sql))
            except (TypeError, ValueError, ValidationError) as exc:
                # Corrupt/non-conforming stored row — skip rather than abort
                # the whole enumeration (and the import that seeds off it).
                # A live-agent memory exceeding the bundle import limits is an
                # expected, non-actionable skip (per the docstring) and would
                # flood logs at WARNING on every export/import, so per-row
                # detail is DEBUG; one aggregate WARNING below keeps a
                # wholesale model-reconstruction regression observable.
                skipped += 1
                logger.debug("skipping unrepresentable memory row: %s", exc)
                continue

        self.last_skipped_rows = skipped
        if (
            total >= _WHOLESALE_MIN_ROWS
            and skipped >= total * _WHOLESALE_SKIP_RATIO
        ):
            logger.error(
                "iter_all could not reconstruct %d of %d stored rows — "
                "refusing to treat this as an empty/partial result",
                skipped,
                total,
            )
            raise MemoryCorruptionError(
                f"{skipped} of {total} stored memory rows are unrepresentable; "
                "the store looks corrupt or written by an incompatible "
                "version. Refusing a wholesale-silent export/import baseline."
            )
        if skipped:
            logger.warning("iter_all skipped %d unrepresentable memory row(s)", skipped)

        return MemoryBundle(
            sql_pairs=pairs or None,
            schema_docs=docs or None,
        )

    def iter_paginated(
        self,
        *,
        where: dict[str, Any] | None = None,
        page_size: int = 500,
    ) -> Iterator[tuple[str, SqlPair | SchemaDoc]]:
        """Paginated, memory-bounded enumeration of the collection.

        Walks the collection via ``collection.get(limit=N, offset=M)`` and
        yields one ``(kind, model)`` tuple at a time. ``kind`` is
        ``"sql_pair"`` or ``"schema_doc"``. The full store is never
        materialized; only one page of metadata is resident at a time.

        ``where`` is forwarded to ``collection.get`` so callers can push
        kind-filtering into ChromaDB rather than scanning the full
        collection only to discard ~half the rows in Python. Pass
        ``where={"tool_name": "run_sql"}`` for SQL pairs only or
        ``where={"is_text_memory": True}`` for schema docs only. The
        per-row classification in this loop still applies as a defensive
        filter (a corrupt row with unexpected metadata cannot leak
        through), but on a healthy store the where filter halves the work
        per export pass.

        Skipping semantics match :meth:`iter_all` per row (non-dict
        metadata, non-``run_sql`` tool memories, unparseable ``args_json``
        are skipped), but :attr:`last_skipped_rows` records the running
        total across pages so a streaming export still reports the
        ``unrepresentable`` warning even when no single page tripped a
        wholesale-corruption threshold. Wholesale corruption is NOT
        checked here: pagination ratios drift page to page and the rule
        ``iter_all`` enforces (≥ 5 rows, ≥ 90 % skipped) is a one-shot
        snapshot guarantee that does not translate cleanly to an
        incremental scan; the streaming export path documents this gap.
        """
        collection = self._mem._get_collection()
        offset = 0
        skipped_total = 0
        get_kwargs: dict[str, Any] = {
            "include": ["metadatas"],
            "limit": page_size,
        }
        if where is not None:
            get_kwargs["where"] = where
        while True:
            page = collection.get(offset=offset, **get_kwargs)
            ids = page.get("ids") or []
            if not ids:
                break
            metadatas = page.get("metadatas") or []
            offset += len(ids)
            for metadata in metadatas:
                if not isinstance(metadata, dict):
                    skipped_total += 1
                    continue
                try:
                    if metadata.get("is_text_memory"):
                        yield (
                            "schema_doc",
                            SchemaDoc(content=metadata.get("content", "")),
                        )
                        continue
                    if metadata.get("tool_name") != RUN_SQL_TOOL_NAME:
                        continue
                    args = json.loads(metadata.get("args_json", "{}"))
                    sql = args.get("sql")
                    if not sql:
                        continue
                    yield (
                        "sql_pair",
                        SqlPair(question=metadata.get("question", ""), sql=sql),
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    skipped_total += 1
                    logger.debug(
                        "skipping unrepresentable memory row in page: %s", exc
                    )
                    continue
            if len(ids) < page_size:
                # Last (partial) page reached; no more rows to fetch.
                break

        self.last_skipped_rows = skipped_total
        if skipped_total:
            logger.warning(
                "iter_paginated skipped %d unrepresentable memory row(s)",
                skipped_total,
            )

    def clear(self) -> int:
        """Wipe every entry in the configured collection; return how many were deleted.

        Deletes by *every* id the collection reports, so rows with missing or
        non-dict metadata (which ``get_all`` skips) are still removed — a
        "clear everything" must not leave corrupt rows behind.
        """
        collection = self._mem._get_collection()
        ids = collection.get(include=[]).get("ids") or []
        if ids:
            collection.delete(ids=ids)
        return len(ids)

    # --- Raw admin enumeration / mutation -------------------------------------
    # These back the memory-administration MCP tools. They return metadata
    # verbatim (no bundle-shape filtering, unlike ``iter_all``) so the admin
    # layer can surface every stored row — auto-learned tool memories included —
    # with its tracking and provenance keys intact. ``_get_collection()`` stays
    # confined to this module (see module docstring).

    @staticmethod
    def _coerce_embedding(raw: Any) -> list[float] | None:
        """Convert a Chroma embedding (often a numpy array) to plain floats.

        Returns ``None`` when no embedding is present so callers can omit the
        field rather than emit an empty vector.
        """
        if raw is None:
            return None
        try:
            vec = [float(x) for x in raw]
        except (TypeError, ValueError) as exc:
            # A non-numeric embedding is corrupt, not absent; both map to None
            # (callers omit the field) but log so the two are distinguishable
            # in the server logs rather than silently identical.
            logger.debug("skipping unrepresentable embedding vector: %s", exc)
            return None
        return vec or None

    def get_all(self, *, include_embeddings: bool = False) -> list[MemoryRecord]:
        """Return every stored row as a :class:`MemoryRecord`.

        Rows whose metadata is missing/non-dict are skipped (they cannot be
        addressed or classified); everything else is returned verbatim.
        """
        collection = self._mem._get_collection()
        include = ["metadatas", "embeddings"] if include_embeddings else ["metadatas"]
        got = collection.get(include=include)
        ids = got.get("ids") or []
        metadatas = got.get("metadatas") or []
        embeddings = got.get("embeddings") if include_embeddings else None

        records: list[MemoryRecord] = []
        for i, memory_id in enumerate(ids):
            metadata = metadatas[i] if i < len(metadatas) else None
            if not isinstance(metadata, dict):
                continue
            embedding = None
            if embeddings is not None and i < len(embeddings):
                embedding = self._coerce_embedding(embeddings[i])
            records.append(
                MemoryRecord(
                    memory_id=memory_id, metadata=dict(metadata), embedding=embedding
                )
            )
        return records

    def get_one(
        self, memory_id: str, *, include_embedding: bool = False
    ) -> MemoryRecord | None:
        """Return a single row by id, or ``None`` if it does not exist."""
        collection = self._mem._get_collection()
        include = ["metadatas", "embeddings"] if include_embedding else ["metadatas"]
        got = collection.get(ids=[memory_id], include=include)
        ids = got.get("ids") or []
        if not ids:
            return None
        metadatas = got.get("metadatas") or []
        metadata = metadatas[0] if metadatas else None
        if not isinstance(metadata, dict):
            metadata = {}
        embedding = None
        if include_embedding:
            embeddings = got.get("embeddings")
            if embeddings is not None and len(embeddings) > 0:
                embedding = self._coerce_embedding(embeddings[0])
        return MemoryRecord(
            memory_id=ids[0], metadata=dict(metadata), embedding=embedding
        )

    def delete_ids(self, ids: list[str]) -> int:
        """Delete the given ids; return how many were actually removed.

        Chroma's ``delete`` is a no-op for unknown ids and reports nothing, so we
        read back which of the requested ids existed, delete those, then re-read
        to confirm they are gone. The returned count reflects what actually left
        the store (present-before minus still-present-after) — never an
        optimistic "we asked to delete N" count, so a partial-delete failure is
        not reported as a clean success.
        """
        if not ids:
            return 0
        collection = self._mem._get_collection()
        present = collection.get(ids=ids, include=[]).get("ids") or []
        if not present:
            return 0
        collection.delete(ids=present)
        still_present = collection.get(ids=present, include=[]).get("ids") or []
        return len(present) - len(still_present)
