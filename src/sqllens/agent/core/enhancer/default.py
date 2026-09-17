"""
Default LLM context enhancer implementation using AgentMemory.

This implementation enriches the system prompt with relevant memories
based on the user's initial message.
"""

import logging
from typing import TYPE_CHECKING, List, Optional

from sqllens.runtime import get_effective_settings

from .base import LlmContextEnhancer

logger = logging.getLogger(__name__)

_DEFAULT_SIMILARITY_THRESHOLD = 0.7
# Injected question->SQL pairs: how many, which tool, and how much SQL each may
# add to the system prompt. Longer statements are cut with a visible marker.
_CONTEXT_PAIR_LIMIT = 5
_CONTEXT_PAIR_TOOL = "run_sql"
_CONTEXT_PAIR_MAX_SQL_CHARS = 8000

if TYPE_CHECKING:
    from ..user.models import User
    from ..llm.models import LlmMessage
    from ...capabilities.agent_memory import (
        AgentMemory,
        TextMemorySearchResult,
        ToolMemory,
        ToolMemorySearchResult,
    )


class DefaultLlmContextEnhancer(LlmContextEnhancer):
    """Default enhancer that uses AgentMemory to add relevant context.

    This enhancer searches the agent's memory for relevant examples and
    tool use patterns based on the user's message, and adds them to the
    system prompt.

    Example:
        agent = Agent(
            llm_service=...,
            agent_memory=agent_memory,
            llm_context_enhancer=DefaultLlmContextEnhancer(agent_memory)
        )
    """

    def __init__(
        self,
        agent_memory: Optional["AgentMemory"] = None,
        *,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        context_similarity_threshold: Optional[float] = None,
    ):
        """Initialize with optional agent memory and injection thresholds.

        Args:
            agent_memory: Optional AgentMemory instance. If not provided,
                         enhancement will be skipped.
            similarity_threshold: The strict hit bar, used when no per-request
                         profile overrides it. Text memories at or above this
                         score are injected.
            context_similarity_threshold: The opt-in context floor. ``None`` (the
                         default) turns the context tier off. When set below the
                         effective strict bar, text memories and saved
                         question->SQL pairs scoring at or above it are injected as
                         related context. When it is not below the strict bar the
                         tier stays off.
        """
        self.agent_memory = agent_memory
        self.similarity_threshold = similarity_threshold
        self.context_similarity_threshold = context_similarity_threshold

    async def enhance_system_prompt(
        self, system_prompt: str, user_message: str, user: "User"
    ) -> str:
        """Enhance system prompt with relevant memories.

        Searches agent memory for relevant text memories based on the
        user's message and adds them to the system prompt.

        Args:
            system_prompt: The original system prompt
            user_message: The initial user message
            user: The user making the request

        Returns:
            Enhanced system prompt with relevant examples from memory
        """
        if not self.agent_memory:
            return system_prompt

        try:
            # Import here to avoid circular dependency
            from ..tool import ToolContext
            import uuid

            # Create a temporary context for memory search
            context = ToolContext(
                user=user,
                conversation_id="temp",
                request_id=str(uuid.uuid4()),
                agent_memory=self.agent_memory,
            )

            # A per-request profile may override the strict bar; the search
            # tool honors it too, so both memory paths agree on one value.
            effective = get_effective_settings()
            strict = (
                effective.similarity_threshold
                if effective is not None
                else self.similarity_threshold
            )
            floor = self.context_similarity_threshold
            tier_enabled = floor is not None and floor < strict
            text_search_floor = floor if tier_enabled else strict

            # Text memories: at or above the strict bar, or down to the context
            # floor when the tier is on.
            memories: List[
                "TextMemorySearchResult"
            ] = await self.agent_memory.search_text_memories(
                query=user_message, context=context, limit=5,
                similarity_threshold=text_search_floor,
            )

            # Saved question->SQL pairs, only when the tier is on. Pairs at or
            # above the strict bar are included too: the search tool runs on the
            # agent's own rewording of the question, which can miss the best
            # match for the real question. A pair also returned by the search
            # tool is simply seen twice.
            pairs: List["ToolMemorySearchResult"] = []
            if tier_enabled:
                try:
                    tool_hits = await self.agent_memory.search_similar_usage(
                        question=user_message,
                        context=context,
                        limit=_CONTEXT_PAIR_LIMIT,
                        similarity_threshold=floor,
                        tool_name_filter=_CONTEXT_PAIR_TOOL,
                    )
                except Exception:
                    # Keep the text hits already found; only the pairs are lost.
                    logger.warning(
                        "Context pair search failed; injecting text memories only",
                        exc_info=True,
                    )
                    tool_hits = []
                pairs = [
                    r
                    for r in tool_hits
                    if isinstance(r.memory.args, dict)
                    and isinstance(r.memory.args.get("sql"), str)
                    and r.memory.args["sql"].strip()
                ]

            if not memories and not pairs:
                return system_prompt

            # Format memories as context snippets to add to system prompt
            examples_section = "\n\n## Relevant Context from Memory\n\n"
            examples_section += "The following domain knowledge and context from prior interactions may be relevant:\n\n"

            for result in memories:
                memory = result.memory
                examples_section += f"• {memory.content}\n"

            # Pairs render as related examples (prior question and its SQL) —
            # context the model weighs, not a pre-decided tool call.
            for result in pairs:
                examples_section += _format_pair(result.memory)

            # Append examples to system prompt
            return system_prompt + examples_section

        except Exception:
            # If memory search fails, return original prompt
            # Don't fail the entire request due to memory issues
            logger.warning(
                "Failed to enhance system prompt with memories", exc_info=True
            )
            return system_prompt

    async def enhance_user_messages(
        self, messages: list["LlmMessage"], user: "User"
    ) -> list["LlmMessage"]:
        """Enhance user messages.

        The default implementation doesn't modify user messages.
        Override this to add context to user messages if needed.

        Args:
            messages: The list of messages
            user: The user making the request

        Returns:
            Original list of messages (unmodified)
        """
        return messages


def _format_pair(pair: "ToolMemory") -> str:
    """Render one saved pair as a related example with a fenced, capped SQL block."""
    sql = pair.args["sql"].strip()
    if len(sql) > _CONTEXT_PAIR_MAX_SQL_CHARS:
        sql = sql[:_CONTEXT_PAIR_MAX_SQL_CHARS] + "\n-- (SQL truncated)"
    return (
        f'• Related prior question: "{pair.question}". It was answered with this SQL:\n'
        f"```sql\n{sql}\n```\n"
    )
