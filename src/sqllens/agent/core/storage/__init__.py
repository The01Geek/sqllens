"""
Storage domain.

This module provides the core abstractions for conversation storage in the SQL Lens agent.
"""

from .base import ConversationStore
from .models import Conversation, Message

__all__ = [
    "ConversationStore",
    "Conversation",
    "Message",
]
