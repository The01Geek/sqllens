# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``query_database`` MCP tool implementation."""

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
#  - ``UnsafeSqlError`` is surfaced verbatim (actionable safety feedback).
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
    suspension point), so two concurrent first calls cannot both run
    ``build_agent``. A later call whose ``cfg`` differs (by identity) from
    the one that built the agent is still served by the original agent, but
    logs a warning rather than silently honoring a config it is not using.
    """
    global _AGENT, _AGENT_CFG
    if _AGENT is None:
        async with _AGENT_LOCK:
            if _AGENT is None:
                _AGENT = build_agent(cfg)
                _AGENT_CFG = cfg
    if cfg is not _AGENT_CFG:
        logger.warning(
            "query_database called with a different Config than the one that "
            "built the agent; reusing the original agent and ignoring the new "
            "config. Run a separate server instance per database."
        )
    return _AGENT


async def query_database_impl(cfg: Config, question: str) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer."""
    agent = await _agent_for(cfg)
    request_context = RequestContext(headers={}, cookies={}, metadata={})

    components = []
    try:
        async for comp in agent.send_message(request_context, question):
            components.append(comp)
    except UnsafeSqlError as e:
        # Actionable safety feedback, not an infra leak — surface verbatim so
        # the calling agent can correct its SQL. This is the SQL-execution
        # error category, distinct from the internal-error category below.
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
