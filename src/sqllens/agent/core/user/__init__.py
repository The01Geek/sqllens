"""
User domain.

This module provides the core abstractions for user management in the SQL Lens agent.
"""

from .base import UserService
from .models import User
from .resolver import UserResolver
from .request_context import RequestContext

__all__ = [
    "UserService",
    "User",
    "UserResolver",
    "RequestContext",
]
