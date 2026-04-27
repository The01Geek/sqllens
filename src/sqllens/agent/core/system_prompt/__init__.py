"""
System prompt domain.

This module provides the core abstractions for building system prompts in the SQL Lens agent.
"""

from .base import SystemPromptBuilder
from .default import DefaultSystemPromptBuilder

__all__ = [
    "SystemPromptBuilder",
    "DefaultSystemPromptBuilder",
]
