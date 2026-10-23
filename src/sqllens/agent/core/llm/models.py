"""
LLM domain models.

This module contains data models for LLM communication.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..tool.models import ToolCall
from ..user.models import User

# Anthropic `output_config.effort` levels.
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class LlmMessage(BaseModel):
    """Message format for LLM communication."""

    role: str = Field(description="Message role")
    content: str = Field(description="Message content")
    tool_calls: Optional[List[ToolCall]] = Field(default=None)
    tool_call_id: Optional[str] = Field(default=None)


class LlmRequest(BaseModel):
    """Request to LLM service."""

    messages: List[LlmMessage] = Field(description="Messages to send")
    tools: Optional[List[Any]] = Field(
        default=None, description="Available tools"
    )  # Will be ToolSchema but avoiding circular import
    user: User = Field(description="User making the request")
    stream: bool = Field(default=False, description="Whether to stream response")
    # None omits the key: some models (Claude Opus 5.5) reject any temperature.
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    effort: Optional[EffortLevel] = Field(default=None)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    system_prompt: Optional[str] = Field(
        default=None, description="System prompt for the LLM"
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LlmResponse(BaseModel):
    """Response from LLM."""

    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def is_tool_call(self) -> bool:
        """Check if this response contains tool calls."""
        return self.tool_calls is not None and len(self.tool_calls) > 0


class LlmStreamChunk(BaseModel):
    """Streaming chunk from LLM."""

    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    finish_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
