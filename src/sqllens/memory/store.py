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
enumerator (only ``get_recent_*`` with a limit) and no public clear. This is
the documented fallback, deliberately isolated to this module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.memory.schema import MemoryBundle, SchemaDoc, SqlPair, SqlPairsBlock

if TYPE_CHECKING:
    from sqllens.config import Config

RUN_SQL_TOOL_NAME = "run_sql"
_IMPORT_SOURCE = "import"


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
        """
        collection = self._mem._get_collection()
        # Skip embedding vectors/documents (largest per-row payload, unused here).
        metadatas = collection.get(include=["metadatas"]).get("metadatas") or []

        pairs: list[SqlPair] = []
        docs: list[SchemaDoc] = []
        for metadata in metadatas:
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

        return MemoryBundle(
            sql_pairs=SqlPairsBlock(pairs=pairs) if pairs else None,
            schema_docs=docs or None,
        )

    def clear(self) -> None:
        """Wipe every entry in the configured collection."""
        collection = self._mem._get_collection()
        ids = collection.get(include=[]).get("ids") or []
        if ids:
            collection.delete(ids=ids)
