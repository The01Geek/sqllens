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
from collections.abc import Mapping
from typing import Any

from sqllens.agent import Agent, RequestContext
from sqllens.agent.factory import build_agent
from sqllens.config import Config
from sqllens.safety import RlsError, UnsafeSqlError
from sqllens.tools._format import components_to_table

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
# The agent and the ``Config`` that built it are stored as one tuple assigned
# atomically: the cfg-mismatch warning's correctness depends on the two never
# disagreeing, so they cannot be separate globals that a future edit (or a
# half-completed assignment) could let drift apart. ``_AGENT_LOCK`` serializes
# the cold start so ``build_agent`` (an ~80 MB embedding-model download) runs
# exactly once under concurrent HTTP load.
_AGENT_STATE: tuple[Agent, Config] | None = None
_AGENT_LOCK = asyncio.Lock()


async def _agent_for(cfg: Config) -> Agent:
    """Return the process-wide agent, building it exactly once.

    Double-checked locking: the outer ``_AGENT_STATE is None`` test is a
    fast-path optimization that skips the lock once the agent exists;
    correctness comes from the *inner* re-check after awaiting ``_AGENT_LOCK``
    (the only suspension point *in this function*), so two concurrent first
    calls cannot both run ``build_agent``. A later call whose ``cfg`` differs
    is still served by the original agent but logs a warning rather than
    silently honoring a config it is not using.

    The mismatch test is by object identity, not value: ``server.py`` builds
    the FastMCP tool once and closes over a single ``Config`` instance that is
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
            "query_database called with a different Config than the one that "
            "built the agent; reusing the original agent and ignoring the new "
            "config. Run a separate server instance per database."
        )
    return agent


async def query_database_impl(
    cfg: Config, question: str, *, metadata: Mapping[str, Any] | None = None
) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer.

    Backwards-compatible wrapper over :func:`query_database_impl_with_table`
    that drops the structured table. The error taxonomy, sanitization, and
    exact raised messages are identical — they live in the sibling below.
    """
    markdown, _ = await query_database_impl_with_table(
        cfg, question, metadata=metadata
    )
    return markdown


async def query_database_impl_with_table(
    cfg: Config, question: str, *, metadata: Mapping[str, Any] | None = None
) -> tuple[str, dict | None]:
    """Translate ``question`` to SQL, execute, and return ``(markdown, table)``.

    Same agent path and same three error categories as the Markdown-only
    contract: tool-internal failures raise ``_INTERNAL_ERROR_MESSAGE``,
    agent-reported SQL failures raise ``_SQL_EXECUTION_ERROR_PREFIX + answer``,
    and ``UnsafeSqlError`` is re-raised verbatim. ``table`` is ``None`` on the
    error path or whenever no DataFrame is present (apps-aware callers attach
    it to ``_meta``; everyone else ignores it).
    """
    try:
        agent = await _agent_for(cfg)
    except Exception as e:
        # Cold-start failures (DB driver connect, ChromaDB, embedding-model
        # download, bad API key) carry the same host/port/role strings S-10
        # targets. Sanitize them identically: full traceback server-side,
        # stable internal message to the client.
        logger.exception("agent construction failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
    # Per-request metadata (caller-supplied MCP metadata, used by the
    # row-level-security guard) flows in here. dict() copies so a caller's
    # mapping can't be mutated downstream, and an absent/empty mapping keeps
    # the prior empty-context behaviour byte-for-byte.
    request_context = RequestContext(
        headers={}, cookies={}, metadata=dict(metadata or {})
    )

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
    except RlsError as e:
        # Same defensive rationale as the UnsafeSqlError branch above: the
        # vendored RunSqlTool swallows this into a tool result today, so this
        # branch is not exercised by that pipeline — but an RLS block is
        # actionable safety feedback, not an infra leak, so if it ever
        # propagates it must reach the client verbatim, not get collapsed
        # into the sanitized internal-error category below.
        logger.warning("query rejected by row-level-security guard: %s", e)
        raise RuntimeError(str(e)) from e
    except Exception as e:
        # Tool-internal / infrastructure failure. The driver exception string
        # (host, port, db, role) is logged server-side only; the client gets a
        # stable, sanitized message it can distinguish from a SQL error.
        logger.exception("agent.send_message failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e

    answer, is_error, table = components_to_table(components)
    if is_error:
        # Agent-reported query failure — SQL-execution error category. S-10's
        # structural leak (raw exception-string interpolation in the except
        # blocks above) is fixed; this path is different: ``answer`` is the
        # agent's own structured error report, and #14 requires it reach the
        # caller as actionable, categorized detail — collapsing it into the
        # sanitized internal message would defeat the category split's whole
        # purpose. Logged server-side so this branch keeps the same
        # operator-debugging trail as every sibling branch. Heuristically
        # content-scrubbing agent-authored text for possible infra substrings
        # is unspecified by #91 and would risk mangling legitimate SQL detail
        # the calling agent needs, so it is deliberately not attempted here.
        logger.warning("agent reported query failure: %s", answer)
        raise RuntimeError(f"{_SQL_EXECUTION_ERROR_PREFIX}{answer}")
    return answer, table
