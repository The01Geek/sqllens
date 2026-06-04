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

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.memory.schema import MemoryBundle, SchemaDoc, SqlPair, SqlPairsBlock

logger = logging.getLogger("sqllens.memory")

if TYPE_CHECKING:
    from sqllens.config import Config

RUN_SQL_TOOL_NAME = "run_sql"
_IMPORT_SOURCE = "import"

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
        await self._mem.save_tool_usage(
            question=question,
            tool_name=RUN_SQL_TOOL_NAME,
            args={"sql": sql},
            context=self._ctx,
            success=True,
            metadata={"source": _IMPORT_SOURCE},
        )

    async def add_schema_doc(self, content: str) -> None:
        await self._mem.save_text_memory(content, self._ctx)

    def iter_all(self) -> MemoryBundle:
        """Enumerate the collection into a bundle.

        Only the two kinds this package can represent are exported: ``run_sql``
        tool memories carrying a ``sql`` arg (→ SQL pairs) and text memories
        (→ schema docs). Any other tool memory the live agent may have written
        is not representable in the bundle format and is skipped.

        A single corrupt or non-conforming row (unparseable ``args_json``, or a
        value the bundle models reject — e.g. a live-agent memory longer than
        the import limits) is skipped, not fatal: ``iter_all`` is also the
        dedup baseline for ``import_bundle``, so one bad row must not abort
        every export and import.

        Wholesale failure is *not* tolerated: if the collection has a
        meaningful number of rows and (almost) none reconstruct, that is
        systemic corruption / version skew, and a silent empty result would
        report a destroyed backup as success and re-save duplicates on the
        next import. In that case :class:`MemoryCorruptionError` is raised.

        ``last_skipped_rows`` records how many rows the most recent call
        skipped so callers (export / import) can surface a non-fatal
        partial-loss warning.
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
            sql_pairs=SqlPairsBlock(pairs=pairs) if pairs else None,
            schema_docs=docs or None,
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
