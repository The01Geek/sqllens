"""
Agent memory tools.

This module provides agent memory operations through an abstract AgentMemory interface,
allowing for different implementations (local vector DB, remote cloud service, etc.).
The tools access AgentMemory via ToolContext, which is populated by the Agent.
"""

import logging
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from sqllens.agent.core.tool import Tool, ToolContext, ToolResult
from sqllens.agent.capabilities.agent_memory import AgentMemory
from sqllens.agent.components import (
    UiComponent,
    StatusBarUpdateComponent,
    StatusCardComponent,
)


class SaveQuestionToolArgsParams(BaseModel):
    """Parameters for saving question-tool-argument combinations."""

    question: str = Field(description="The original question that was asked")
    tool_name: str = Field(
        description="The name of the tool that was used successfully"
    )
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="The arguments that were passed to the tool",
    )


class SearchSavedCorrectToolUsesParams(BaseModel):
    """Parameters for searching saved tool usage patterns."""

    question: str = Field(
        description="The question to find similar tool usage patterns for"
    )
    limit: Optional[int] = Field(
        default=10, description="Maximum number of results to return"
    )
    similarity_threshold: Optional[float] = Field(
        default=None,
        description=(
            "Minimum similarity score for results (0.0-1.0). "
            "When omitted, the server-configured default is used."
        ),
    )
    tool_name_filter: Optional[str] = Field(
        default=None, description="Filter results to specific tool name"
    )


class SaveTextMemoryParams(BaseModel):
    """Parameters for saving free-form text memories."""

    content: str = Field(description="The text content to save as a memory")


class SaveQuestionToolArgsTool(Tool[SaveQuestionToolArgsParams]):
    """Tool for saving successful question-tool-argument combinations."""

    @property
    def name(self) -> str:
        return "save_question_tool_args"

    @property
    def description(self) -> str:
        return (
            "Save a successful question-tool-argument combination for future reference"
        )

    def get_args_schema(self) -> Type[SaveQuestionToolArgsParams]:
        return SaveQuestionToolArgsParams

    async def execute(
        self, context: ToolContext, args: SaveQuestionToolArgsParams
    ) -> ToolResult:
        """Save the tool usage pattern to agent memory."""
        try:
            await context.agent_memory.save_tool_usage(
                question=args.question,
                tool_name=args.tool_name,
                args=args.args,
                context=context,
                success=True,
            )

            success_msg = (
                f"Successfully saved usage pattern for '{args.tool_name}' tool"
            )
            return ToolResult(
                success=True,
                result_for_llm=success_msg,
                ui_component=UiComponent(
                    rich_component=StatusBarUpdateComponent(
                        status="success",
                        message="Saved to memory",
                        detail=f"Saved pattern for '{args.tool_name}'",
                    ),
                    simple_component=None,
                ),
            )

        except Exception as e:
            error_message = f"Failed to save memory: {str(e)}"
            return ToolResult(
                success=False,
                result_for_llm=error_message,
                ui_component=UiComponent(
                    rich_component=StatusBarUpdateComponent(
                        status="error", message="Failed to save memory", detail=str(e)
                    ),
                    simple_component=None,
                ),
                error=str(e),
            )


class SearchSavedCorrectToolUsesTool(Tool[SearchSavedCorrectToolUsesParams]):
    """Tool for searching saved tool usage patterns."""

    def __init__(self, *, default_similarity_threshold: float = 0.7) -> None:
        super().__init__()
        self._default_similarity_threshold = default_similarity_threshold

    @property
    def name(self) -> str:
        return "search_saved_correct_tool_uses"

    @property
    def description(self) -> str:
        return "Search for similar tool usage patterns based on a question"

    def get_args_schema(self) -> Type[SearchSavedCorrectToolUsesParams]:
        return SearchSavedCorrectToolUsesParams

    async def execute(
        self, context: ToolContext, args: SearchSavedCorrectToolUsesParams
    ) -> ToolResult:
        """Search for similar tool usage patterns."""
        try:
            threshold = (
                args.similarity_threshold
                if args.similarity_threshold is not None
                else self._default_similarity_threshold
            )
            results = await context.agent_memory.search_similar_usage(
                question=args.question,
                context=context,
                limit=args.limit or 10,
                similarity_threshold=threshold,
                tool_name_filter=args.tool_name_filter,
            )

            if not results:
                no_results_msg = (
                    "No similar tool usage patterns found for this question."
                )

                # Symmetric with the hit log below: a miss is observable
                # server-side too, so operators tuning the similarity threshold
                # see both outcomes.
                logger.info(
                    "Agent memory search: no matches (threshold=%.2f)", threshold
                )

                # Emit the hit/miss signal on a STATUS_CARD whose metadata the
                # MCP formatter (tools/_format.components_to_widgets) reads — the
                # same seam used for the run_sql card's executed SQL. Only the
                # aggregate fact of a miss is exposed, never matched contents.
                return ToolResult(
                    success=True,
                    result_for_llm=no_results_msg,
                    ui_component=UiComponent(
                        rich_component=StatusCardComponent(
                            title="Memory Search",
                            status="info",
                            description="No similar patterns found",
                            icon="🧠",
                            metadata={
                                "memory_search": {
                                    "searched": True,
                                    "hit_count": 0,
                                    "top_similarity": None,
                                    "threshold": threshold,
                                }
                            },
                        ),
                        simple_component=None,
                    ),
                )

            # Format results for LLM
            results_text = f"Found {len(results)} similar tool usage pattern(s):\n\n"
            for i, result in enumerate(results, 1):
                memory = result.memory
                results_text += f"{i}. {memory.tool_name} (similarity: {result.similarity_score:.2f})\n"
                results_text += f"   Question: {memory.question}\n"
                results_text += f"   Args: {memory.args}\n\n"

            logger.info(f"Agent memory search results: {results_text.strip()}")

            # Coerce to a plain float: the score may arrive as a numpy float
            # from the ChromaDB backend, which would not survive JSON
            # serialization into the MCP _meta channel.
            top_similarity = float(max(result.similarity_score for result in results))

            # Emit the hit signal on a STATUS_CARD whose metadata the MCP
            # formatter reads (same seam as the miss path and the run_sql card).
            # Only aggregate counts/scores are exposed here — the matched
            # questions/args stay server-side (issue #168 out-of-scope).
            return ToolResult(
                success=True,
                result_for_llm=results_text.strip(),
                ui_component=UiComponent(
                    rich_component=StatusCardComponent(
                        title="Memory Search",
                        status="success",
                        description=f"Found {len(results)} similar pattern(s)",
                        icon="🧠",
                        metadata={
                            "memory_search": {
                                "searched": True,
                                "hit_count": len(results),
                                "top_similarity": top_similarity,
                                "threshold": threshold,
                            }
                        },
                    ),
                    simple_component=None,
                ),
            )

        except Exception as e:
            error_message = f"Failed to search memories: {str(e)}"
            return ToolResult(
                success=False,
                result_for_llm=error_message,
                ui_component=UiComponent(
                    rich_component=StatusBarUpdateComponent(
                        status="error", message="Failed to search memory", detail=str(e)
                    ),
                    simple_component=None,
                ),
                error=str(e),
            )


class SaveTextMemoryTool(Tool[SaveTextMemoryParams]):
    """Tool for saving free-form text memories."""

    @property
    def name(self) -> str:
        return "save_text_memory"

    @property
    def description(self) -> str:
        return "Save free-form text memory for important insights, observations, or context"

    def get_args_schema(self) -> Type[SaveTextMemoryParams]:
        return SaveTextMemoryParams

    async def execute(
        self, context: ToolContext, args: SaveTextMemoryParams
    ) -> ToolResult:
        """Save a text memory to agent memory."""
        try:
            text_memory = await context.agent_memory.save_text_memory(
                content=args.content, context=context
            )

            success_msg = (
                f"Successfully saved text memory with ID: {text_memory.memory_id}"
            )
            return ToolResult(
                success=True,
                result_for_llm=success_msg,
                ui_component=UiComponent(
                    rich_component=StatusBarUpdateComponent(
                        status="success",
                        message="Saved text memory",
                        detail=f"ID: {text_memory.memory_id}",
                    ),
                    simple_component=None,
                ),
            )

        except Exception as e:
            error_message = f"Failed to save text memory: {str(e)}"
            return ToolResult(
                success=False,
                result_for_llm=error_message,
                ui_component=UiComponent(
                    rich_component=StatusBarUpdateComponent(
                        status="error",
                        message="Failed to save text memory",
                        detail=str(e),
                    ),
                    simple_component=None,
                ),
                error=str(e),
            )
