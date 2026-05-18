# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``query_database`` MCP tool implementation.

The FastMCP tool wrapper (``mcp.server.fastmcp`` — the official MCP SDK, not
the separately-distributed standalone ``fastmcp`` package) in ``server.py``
re-raises any exception from this module and maps it to an ``isError: true``
result, currently formatting the client text as
``Error executing tool query_database: <message>``. Our contract is therefore
the *raised message* (the categorized text below), which the client receives
as the suffix after that wrapper prefix — the category split stays observable
because the forms here remain mutually distinguishable under it.
"""

from __future__ import annotations

import asyncio
import logging

from sqllens.agent import Agent, RequestContext
from sqllens.agent.factory import build_agent
from sqllens.config import Config
from sqllens.safety import UnsafeSqlError
from sqllens.tools._format import components_to_markdown

logger = logging.getLogger("sqllens.tools.query_database")

# Client-facing error taxonomy. The MCP wrapper collapses every failure into
# one ``isError: true`` result, so the *message* is the only category signal
# the caller gets — keep both forms named here so the split stays observable
# in one place:
#  - tool-internal / infrastructure failures get the stable sanitized message
#    (driver exceptions carry host/port/db/role; the full traceback is logged
#    server-side instead of echoed to the MCP client),
#  - SQL-execution failures the agent reported get a recognizable prefix,
#  - ``UnsafeSqlError`` is surfaced verbatim — issue #91 mandates the original
#    safety message reach the client unaltered, so this form is deliberately
#    *not* prefixed; it stays distinguishable by its own recognizable text
#    ("refusing to execute non-SELECT SQL: ..."), not by a constant prefix.
_INTERNAL_ERROR_MESSAGE = "internal error; see server logs"
_SQL_EXECUTION_ERROR_PREFIX = "SQL execution error: "

# Lazy-built singleton — first call wires the agent, subsequent calls reuse it.
# ``_AGENT_CFG`` records the ``Config`` that built it so a later call with a
# different config gets an explicit signal instead of silent wrong-agent reuse.
# ``_AGENT_LOCK`` serializes the cold start so ``build_agent`` (an ~80 MB
# embedding-model download) runs exactly once under concurrent HTTP load.
_AGENT: Agent | None = None
_AGENT_CFG: Config | None = None
_AGENT_LOCK = asyncio.Lock()


async def _agent_for(cfg: Config) -> Agent:
    """Return the process-wide agent, building it exactly once.

    Double-checked locking: the outer ``_AGENT is None`` test is a fast-path
    optimization that skips the lock once the agent exists; correctness comes
    from the *inner* re-check after awaiting ``_AGENT_LOCK`` (the only
    suspension point *in this function*), so two concurrent first calls cannot
    both run ``build_agent``. A later call whose ``cfg`` differs is still
    served by the original agent but logs a warning rather than silently
    honoring a config it is not using.

    The mismatch test is by object identity, not value: ``server.py`` builds
    the FastMCP tool once and closes over a single ``Config`` instance that is
    passed to every call, so identity is stable for a correctly-run server and
    a *different* object genuinely means a second config was introduced. Do
    not "fix" this to ``!=`` — value-equality would false-warn on a benign
    config reload that produced an equal-but-distinct object.
    """
    global _AGENT, _AGENT_CFG
    if _AGENT is None:
        async with _AGENT_LOCK:
            if _AGENT is None:
                _AGENT = build_agent(cfg)
                _AGENT_CFG = cfg
    if _AGENT_CFG is not None and cfg is not _AGENT_CFG:
        logger.warning(
            "query_database called with a different Config than the one that "
            "built the agent; reusing the original agent and ignoring the new "
            "config. Run a separate server instance per database."
        )
    return _AGENT


async def query_database_impl(cfg: Config, question: str) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer."""
    try:
        agent = await _agent_for(cfg)
    except Exception as e:
        # Cold-start failures (DB driver connect, ChromaDB, embedding-model
        # download, bad API key) carry the same host/port/role strings S-10
        # targets. Sanitize them identically: full traceback server-side,
        # stable internal message to the client.
        logger.exception("agent construction failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
    request_context = RequestContext(headers={}, cookies={}, metadata={})

    components = []
    try:
        async for comp in agent.send_message(request_context, question):
            components.append(comp)
    except UnsafeSqlError as e:
        # Defensive path: the current vendored agent catches a read-only-guard
        # violation inside its SQL tool (RunSqlTool.execute's broad
        # ``except Exception`` at agent/tools/run_sql.py:182) and feeds it back
        # as a tool result rather than propagating UnsafeSqlError out of
        # send_message, so this branch is not exercised by that pipeline today
        # (a real guard violation surfaces via the is_error path below). It is
        # kept because UnsafeSqlError *is* actionable safety feedback (not an
        # infra leak): if it ever propagates (a direct guard call, a future
        # code path), it must reach the client verbatim, distinct from the
        # sanitized internal-error category below.
        logger.warning("query rejected by read-only guard: %s", e)
        raise RuntimeError(str(e)) from e
    except Exception as e:
        # Tool-internal / infrastructure failure. The driver exception string
        # (host, port, db, role) is logged server-side only; the client gets a
        # stable, sanitized message it can distinguish from a SQL error.
        logger.exception("agent.send_message failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e

    answer, is_error = components_to_markdown(components)
    if is_error:
        # Agent-reported query failure — SQL-execution error category.
        raise RuntimeError(f"{_SQL_EXECUTION_ERROR_PREFIX}{answer}")
    return answer
