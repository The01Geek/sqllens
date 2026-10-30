"""
SQL runner capability interface.

This module contains the abstract base class for SQL execution.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import pandas as pd

from .models import RunSqlToolArgs

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext


class SqlRunner(ABC):
    """Interface for SQL execution with different implementations."""

    @abstractmethod
    async def run_sql(
        self, args: RunSqlToolArgs, context: "ToolContext", *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        """Execute SQL query and return results as a DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context
            require_base_tables: Verify referenced relations and called routines on
                the execution connection and refuse views, unverified relation kinds,
                child tables, calls whose names match database-defined routines, and
                failed catalog lookups before running SQL.
                Required by RLS; implementations must not ignore this flag.

        Returns:
            DataFrame with query results

        Raises:
            Exception: If query execution fails
        """
        pass
