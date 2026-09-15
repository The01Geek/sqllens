"""
Default LLM context enhancer implementation using AgentMemory.

This implementation enriches the system prompt with relevant memories
based on the user's initial message.
"""

from typing import TYPE_CHECKING, List, Optional
from .base import LlmContextEnhancer

# Default shared by both thresholds on this enhancer seam, so the near-match
# band is empty unless a caller opts in: a direct DefaultLlmContextEnhancer(...)
# gets similarity_threshold == context_similarity_threshold by default, and
# bumping this default moves both together. This parallels the module-level
# _DEFAULT_SIMILARITY_THRESHOLD in sqllens.config (which the factory feeds in
# from MemoryConfig). The two constants are defined per module — config must not
# import the vendored agent tree, nor the tree config — so each independently
# keeps its own pair of field defaults coupled; they are not one global constant.
_DEFAULT_SIMILARITY_THRESHOLD = 0.7

if TYPE_CHECKING:
    from ..user.models import User
    from ..llm.models import LlmMessage
    from ...capabilities.agent_memory import (
        AgentMemory,
        TextMemorySearchResult,
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
        context_similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ):
        """Initialize with optional agent memory and injection thresholds.

        Args:
            agent_memory: Optional AgentMemory instance. If not provided,
                         enhancement will be skipped.
            similarity_threshold: The strict hit bar. Text memories at or above
                         this score are injected, and it is the upper (excluded)
                         bound of the permissive near-match band.
            context_similarity_threshold: The lower, opt-in band floor. When it
                         is below similarity_threshold, memories scoring in
                         [context_similarity_threshold, similarity_threshold) are
                         injected as related context (both question->SQL pairs and
                         text/schema-doc memories). When it is >= similarity_threshold
                         (the default) the band is empty and injection is unchanged.
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

            # The band sits strictly below the strict hit bar; when the opt-in
            # floor is not below it, the band is empty.
            strict = self.similarity_threshold
            context_floor = self.context_similarity_threshold
            band_enabled = context_floor < strict
            # Search text at the lower of the two bars: the band floor when the
            # band is enabled, else the strict bar (the value similarity_threshold
            # now controls — don't restore the old hard-coded 0.7).
            text_search_floor = min(context_floor, strict)

            # Search for relevant text memories based on user message. Text
            # memories at or above the strict bar keep being injected; the band
            # extends this down to the context floor when enabled.
            memories: List[
                "TextMemorySearchResult"
            ] = await self.agent_memory.search_text_memories(
                query=user_message, context=context, limit=5,
                similarity_threshold=text_search_floor,
            )

            # Gather near-match question->SQL pairs, but only the band: pairs at
            # or above the strict bar stay reusable through the search tool and
            # must not be duplicated here. Only run this extra search when the
            # band is enabled, so a non-opted-in deployment issues no extra
            # memory search, as before.
            band_pairs: List["ToolMemorySearchResult"] = []
            if band_enabled:
                tool_hits = await self.agent_memory.search_similar_usage(
                    question=user_message, context=context, limit=5,
                    similarity_threshold=context_floor,
                )
                band_pairs = [
                    r
                    for r in tool_hits
                    if r.similarity_score < strict
                    and isinstance(r.memory.args, dict)
                    and r.memory.args.get("sql")
                ]

            if not memories and not band_pairs:
                return system_prompt

            # Format memories as context snippets to add to system prompt
            examples_section = "\n\n## Relevant Context from Memory\n\n"
            examples_section += "The following domain knowledge and context from prior interactions may be relevant:\n\n"

            for result in memories:
                memory = result.memory
                examples_section += f"• {memory.content}\n"

            # Near-match pairs render as related example text (prior question and
            # its SQL) — as context the model weighs, not a pre-decided tool call.
            for result in band_pairs:
                pair = result.memory
                examples_section += (
                    f'• Related prior question — "{pair.question}" — '
                    f"was answered with this SQL: {pair.args['sql']}\n"
                )

            # Append examples to system prompt
            return system_prompt + examples_section

        except Exception as e:
            # If memory search fails, return original prompt
            # Don't fail the entire request due to memory issues
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to enhance system prompt with memories: {e}")
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
