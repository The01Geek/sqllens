"""SQL Lens NL-to-SQL agent.

This package contains the agent framework, components, capabilities, and
integration layers used to translate natural-language questions into SQL.
"""

from sqllens.agent.core import (
    Agent,
    Conversation,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmService,
    LlmStreamChunk,
    Message,
    SystemPromptBuilder,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSchema,
    User,
)
from sqllens.agent.core.registry import ToolRegistry
from sqllens.agent.core.user import UserResolver
from sqllens.agent.core.user.request_context import RequestContext

__all__ = [
    "Agent",
    "Conversation",
    "LlmMessage",
    "LlmRequest",
    "LlmResponse",
    "LlmService",
    "LlmStreamChunk",
    "Message",
    "RequestContext",
    "SystemPromptBuilder",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSchema",
    "User",
    "UserResolver",
]
