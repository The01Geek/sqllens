# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Process-wide agent singleton shared by the MCP tool wrappers.

Both ``query_database`` and ``visualize_data`` translate natural language to
SQL through the *same* agent: it is constructed once per process, wired with
both ``RunSqlTool`` and ``EmitChartTool``, and reused across requests. This
module owns that singleton (the agent object graph, the cold-start lock, and
the boot-time memory warm) so the two tool wrappers cannot accidentally build
two competing agents.

The client-facing error taxonomy is deliberately *not* here — it stays in
``query_database`` so it is defined in one place; ``visualize_data`` re-imports
those constants. This module only constructs and caches the agent.
"""

from __future__ import annotations

import asyncio
import logging

from sqllens.agent import Agent, ToolContext, User
from sqllens.agent.factory import build_agent
from sqllens.config import Config

logger = logging.getLogger("sqllens.tools._agent")

# Lazy-built singleton — first call wires the agent, subsequent calls reuse it.
# The agent and the ``Config`` that built it are stored as one tuple assigned
# atomically: the cfg-mismatch warning's correctness depends on the two never
# disagreeing, so they cannot be separate globals that a future edit (or a
# half-completed assignment) could let drift apart. ``_AGENT_LOCK`` serializes
# the cold start so the agent object graph is wired exactly once under
# concurrent HTTP load. Note ``build_agent`` itself only constructs objects:
# ``ChromaAgentMemory.__init__`` does no I/O, so the ChromaDB open and the
# ~80 MB embedding-model download are *not* triggered here — they fire lazily
# the first time a memory method touches the collection (see ``_warm_memory``,
# which forces that materialization eagerly at server boot).
_AGENT_STATE: tuple[Agent, Config] | None = None
_AGENT_LOCK = asyncio.Lock()


async def get_agent(cfg: Config) -> Agent:
    """Return the process-wide agent, building it exactly once.

    Double-checked locking: the outer ``_AGENT_STATE is None`` test is a
    fast-path optimization that skips the lock once the agent exists;
    correctness comes from the *inner* re-check after awaiting ``_AGENT_LOCK``
    (the only suspension point *in this function*), so two concurrent first
    calls cannot both run ``build_agent``. A later call whose ``cfg`` differs
    is still served by the original agent but logs a warning rather than
    silently honoring a config it is not using.

    The mismatch test is by object identity, not value: ``server.py`` builds
    the FastMCP tools once and closes over a single ``Config`` instance that is
    passed to every call, so identity is stable for a correctly-run server and
    a *different* object genuinely means a second config was introduced. Do
    not "fix" this to ``!=`` — value-equality would false-warn on a benign
    config reload that produced an equal-but-distinct object.
    """
    global _AGENT_STATE
    if _AGENT_STATE is None:
        async with _AGENT_LOCK:
            if _AGENT_STATE is None:
                _AGENT_STATE = (build_agent(cfg), cfg)
    agent, built_cfg = _AGENT_STATE
    if cfg is not built_cfg:
        logger.warning(
            "agent called with a different Config than the one that built it; "
            "reusing the original agent and ignoring the new config. Run a "
            "separate server instance per database."
        )
    return agent


# Constants for the boot-time memory warm touch. ``get_recent_memories`` is a
# read-only call whose ChromaDB result is discarded; its sole purpose is to
# force ``_get_collection()`` → ``_get_embedding_function()`` so the ChromaDB
# open and the ~80 MB ``DefaultEmbeddingFunction`` model download happen at
# server boot rather than on the first query call. The vendored Chroma impl
# ignores ``context``, but a valid ``ToolContext`` is still built so the public
# contract is honored and any future memory backend behaves.
_WARMUP_USER_ID = "sqllens-warmup"


async def _warm_memory(agent: Agent) -> None:
    """Force the agent's vector memory to materialize (eager cold-start).

    Calls one read-only memory method so ``ChromaAgentMemory`` opens its
    persistent client and instantiates the default embedding function (the
    ~80 MB model download). Without this, ``build_agent`` only wires objects
    and the download still lands on the first query. The returned memories are
    discarded — only the side effect (collection + embedding model resident)
    matters. Propagates any failure to the caller.
    """
    warmup_user = User(id=_WARMUP_USER_ID, group_memberships=[])
    context = ToolContext(
        user=warmup_user,
        conversation_id="warmup",
        request_id="warmup",
        agent_memory=agent.agent_memory,
    )
    await agent.agent_memory.get_recent_memories(context, limit=1)


async def prime_agent(cfg: Config) -> None:
    """Eagerly build, cache, and warm the process-wide agent.

    Delegates to ``get_agent`` so the agent built at server startup *is* the
    same ``_AGENT_STATE`` singleton the request path serves — the object graph
    is constructed once, not once per consumer. Then runs ``_warm_memory`` to
    force the otherwise-lazy ChromaDB open and ~80 MB embedding-model download
    so that cold-start cost is paid at boot instead of on the first query
    (the substantive goal of issue #116).

    Propagates any build *or* warm failure to the caller (which decides
    whether a failed warmup should block startup); ``get_agent``'s own retry
    contract is unchanged — a failed warm leaves ``_AGENT_STATE`` populated
    (the agent built fine; only the memory touch failed), so the request path
    still functions and simply re-attempts the lazy materialization itself.
    """
    agent = await get_agent(cfg)
    await _warm_memory(agent)
