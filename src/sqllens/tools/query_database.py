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

import logging
from collections.abc import Mapping
from typing import Any

from sqllens.agent import RequestContext
from sqllens.config import RESERVED_METADATA_KEYS, Config
from sqllens.safety import RlsError, UnsafeSqlError
from sqllens.tools._agent import get_agent, prime_agent
from sqllens.tools._format import components_to_widgets

# ``prime_agent`` lives in ``tools/_agent.py`` but the transport-layer warmup
# (``transport/http.py``) and several tests import it from here — keep it in
# ``__all__`` so it is a stable re-export, not an implementation detail.
__all__ = [
    "prime_agent",
    "query_database_impl",
    "query_database_impl_with_table",
    "query_database_impl_with_widgets",
]

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

# Internal agent-control keys that the agent reads off
# ``request_context.metadata`` (e.g. ``starter_ui_request`` at
# agent/core/agent/agent.py, ``ui_features_available`` injected into the tool
# context). Caller-supplied MCP ``_meta`` now flows into that same mapping for
# row-level-security dynamic values, so these keys are stripped at the boundary
# — untrusted request metadata must not be able to steer internal agent
# control flow, only supply RLS predicate values. The same set is also forbidden
# at config load for ``value_from_metadata`` (see sqllens.config) so a typo
# cannot create a rule that always resolves to a key that will always be
# stripped here. Single source of truth lives in sqllens.config.
_RESERVED_METADATA_KEYS = RESERVED_METADATA_KEYS

def _append_sql_block(markdown: str, query_info: dict | None) -> str:
    """Append the executed SQL as a fenced ``sql`` block (text fallback).

    Structured ``query_info`` in ``_meta`` is the source of truth; this is the
    plain-text rendering for non-apps clients. Falsy ``query_info`` returns
    markdown unchanged byte-for-byte (show_details off / no SQL ran).
    """
    if not query_info:
        return markdown
    sql = query_info.get("sql")
    if not sql:
        return markdown
    return f"{markdown}\n\n**Executed SQL:**\n\n```sql\n{sql}\n```"


async def query_database_impl(
    cfg: Config, question: str, *, metadata: Mapping[str, Any] | None = None
) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer.

    Backwards-compatible wrapper over :func:`query_database_impl_with_widgets`
    that drops the structured payloads. The error taxonomy, sanitization, and
    exact raised messages are identical — they live in the sibling below.
    """
    markdown, _, _, _ = await query_database_impl_with_widgets(
        cfg, question, metadata=metadata
    )
    return markdown


async def query_database_impl_with_table(
    cfg: Config, question: str, *, metadata: Mapping[str, Any] | None = None
) -> tuple[str, dict | None, dict | None]:
    """Translate ``question`` to SQL, execute, return ``(markdown, table, query_info)``.

    Thin wrapper over :func:`query_database_impl_with_widgets` that drops the
    chart payload. The agent path, error taxonomy, and exact raised messages
    are identical — they live in the sibling below.
    """
    markdown, table, query_info, _ = await query_database_impl_with_widgets(
        cfg, question, metadata=metadata
    )
    return markdown, table, query_info


async def query_database_impl_with_widgets(
    cfg: Config, question: str, *, metadata: Mapping[str, Any] | None = None
) -> tuple[str, dict | None, dict | None, dict | None]:
    """Translate ``question`` to SQL, execute, return ``(markdown, table, query_info, chart)``.

    The single agent path behind the consolidated ``query_database`` MCP tool.
    One ``agent.send_message`` run is buffered and collapsed in a single pass by
    :func:`~sqllens.tools._format.components_to_widgets`, which yields the
    Markdown answer (DataFrame tables + answer text, plus the fenced SQL block
    when ``agent.show_details`` is on), the structured table payload, the
    executed-SQL ``query_info``, and the structured chart payload when the
    agent emitted a ``ChartComponent``.

    Three error categories, unchanged: tool-internal failures raise
    ``_INTERNAL_ERROR_MESSAGE``, agent-reported SQL failures raise
    ``_SQL_EXECUTION_ERROR_PREFIX + answer``, and ``UnsafeSqlError`` is
    re-raised verbatim. ``table`` and ``chart`` are ``None`` on the error path
    or whenever the corresponding component is absent (apps-aware callers attach
    whichever is present to ``_meta``; everyone else ignores them and reads the
    Markdown). ``query_info`` carries the executed SQL when
    ``agent.show_details`` is on, ``None`` otherwise — and when present, the
    same SQL is also appended to ``markdown`` as a fenced ``sql`` block so
    plain-text clients see it too.
    """
    try:
        agent = await get_agent(cfg)
    except Exception as e:
        # Cold-start failures (DB driver connect, ChromaDB, embedding-model
        # download, bad API key) carry the same host/port/role strings S-10
        # targets. Sanitize them identically: full traceback server-side,
        # stable internal message to the client.
        logger.exception("agent construction failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
    # Per-request metadata (caller-supplied MCP metadata, used by the
    # row-level-security guard) flows in here. Reserved internal-control keys
    # are stripped so untrusted request metadata cannot steer agent control
    # flow; the dict comprehension also copies so a caller's mapping can't be
    # mutated downstream, and an absent/empty mapping keeps the prior
    # empty-context behaviour byte-for-byte.
    safe_metadata = {
        k: v
        for k, v in (metadata or {}).items()
        if k not in _RESERVED_METADATA_KEYS
    }
    request_context = RequestContext(
        headers={}, cookies={}, metadata=safe_metadata
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

    answer, is_error, table, query_info, chart = components_to_widgets(components)
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
    return _append_sql_block(answer, query_info), table, query_info, chart
