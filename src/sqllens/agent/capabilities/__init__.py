"""
Capabilities module.

This package contains abstractions for tool capabilities - reusable utilities
that tools can compose via dependency injection.
"""

from .file_system import FileSearchMatch, FileSystem
from .sql_runner import RunSqlToolArgs, SqlRunner

__all__ = [
    "FileSystem",
    "FileSearchMatch",
    "SqlRunner",
    "RunSqlToolArgs",
]
