"""``query_database`` MCP tool implementation."""

from __future__ import annotations

import logging

from sqllens.agent import Agent, RequestContext
from sqllens.agent.factory import build_agent
from sqllens.config import Config
from sqllens.tools._format import components_to_markdown

logger = logging.getLogger("sqllens.tools.query_database")

# Lazy-built singleton — first call wires the agent, subsequent calls reuse it.
_AGENT: Agent | None = None


def _agent_for(cfg: Config) -> Agent:
    global _AGENT
    if _AGENT is None:
        _AGENT = build_agent(cfg)
    return _AGENT


async def query_database_impl(cfg: Config, question: str) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer."""
    agent = _agent_for(cfg)
    request_context = RequestContext(headers={}, cookies={}, metadata={})

    components = []
    try:
        async for comp in agent.send_message(request_context, question):
            components.append(comp)
    except Exception as e:
        logger.exception("agent.send_message failed")
        # Surface as an MCP tool error — the calling agent should see structured failure.
        raise RuntimeError(f"query_database failed: {e}") from e

    answer, is_error = components_to_markdown(components)
    if is_error:
        raise RuntimeError(answer)
    return answer
