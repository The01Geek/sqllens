# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``visualize_data`` MCP tool implementation.

Parallel to :mod:`sqllens.tools.query_database`: same shared process-wide
agent (``tools/_agent.py``), same ``agent.send_message`` path, same
client-facing error taxonomy. The only difference is the UI surface — this
tool collects a ``ChartComponent`` from the stream via
:func:`components_to_chart` instead of a DataFrame.

The error taxonomy constants are deliberately *re-imported* from
``query_database`` rather than redefined, so the sanitized-internal /
SQL-execution-prefix / verbatim-``UnsafeSqlError`` split stays defined in
exactly one place and the two tools cannot drift apart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqllens.agent import RequestContext
from sqllens.config import Config
from sqllens.safety import UnsafeSqlError
from sqllens.tools._agent import get_agent
from sqllens.tools._format import components_to_chart
from sqllens.tools.query_database import (
    _INTERNAL_ERROR_MESSAGE,
    _SQL_EXECUTION_ERROR_PREFIX,
    strip_reserved_metadata,
)

logger = logging.getLogger("sqllens.tools.visualize_data")


async def visualize_data_impl_with_chart(
    cfg: Config,
    question: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
) -> tuple[str, dict | None]:
    """Translate ``question`` to SQL, execute, and return ``(markdown, chart)``.

    Same agent path and same three error categories as ``query_database``:
    tool-internal failures raise ``_INTERNAL_ERROR_MESSAGE``, agent-reported
    SQL failures raise ``_SQL_EXECUTION_ERROR_PREFIX + answer``, and
    ``UnsafeSqlError`` is re-raised verbatim. Errors *raise*; they do not
    return ``(markdown, None)``. On a successful result, ``chart`` is ``None``
    whenever no ChartComponent is present (apps-aware callers attach it to
    ``_meta``; everyone else ignores it and reads the Markdown).

    ``metadata`` (caller-supplied MCP ``_meta``, reserved keys stripped) and
    ``conversation_id`` are threaded through identically to ``query_database``
    so this tool reads RLS values and supports multi-turn conversations too.
    """
    try:
        agent = await get_agent(cfg)
    except Exception as e:
        # Cold-start failures (DB driver connect, ChromaDB, embedding-model
        # download, bad API key) carry host/port/role strings. Sanitize them
        # identically to query_database: full traceback server-side, stable
        # internal message to the client.
        logger.exception("agent construction failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
    request_context = RequestContext(
        headers={}, cookies={}, metadata=strip_reserved_metadata(metadata)
    )

    components = []
    try:
        async for comp in agent.send_message(
            request_context, question, conversation_id=conversation_id
        ):
            components.append(comp)
    except UnsafeSqlError as e:
        # Defensive path, identical contract to query_database: the vendored
        # agent currently converts a read-only-guard violation into a tool
        # result rather than propagating ``UnsafeSqlError`` out of
        # ``send_message``, but if it ever propagates it must reach the client
        # verbatim, distinct from the sanitized internal-error category.
        logger.warning("query rejected by read-only guard: %s", e)
        raise RuntimeError(str(e)) from e
    except Exception as e:
        # Tool-internal / infrastructure failure. The driver exception string
        # (host, port, db, role) is logged server-side only; the client gets a
        # stable, sanitized message it can distinguish from a SQL error.
        logger.exception("agent.send_message failed")
        raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e

    answer, is_error, chart = components_to_chart(components)
    if is_error:
        # Agent-reported query failure — SQL-execution error category, same as
        # query_database so the error taxonomy is identical across both tools.
        logger.warning("agent reported query failure: %s", answer)
        raise RuntimeError(f"{_SQL_EXECUTION_ERROR_PREFIX}{answer}")
    return answer, chart
