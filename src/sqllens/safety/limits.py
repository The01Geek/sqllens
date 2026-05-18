# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-cap enforcement for SqlRunner implementations.

The per-runner adapters (``postgres/mysql/sqlite/sql_runner.py``) stream rows
via ``fetchmany(max_rows + 1)`` and stamp ``df.attrs['truncated']`` themselves
— that is the primary defence against unbounded materialisation. This
``RowCapRunner`` decorator is the **secondary** defence: it post-processes the
returned DataFrame and re-applies the cap, so a future runner that forgets to
stream still cannot return more than ``max_rows`` rows downstream.

The truncation signal lives on ``df.attrs`` (a pandas-standard dict carried by
DataFrames). ``RunSqlTool`` reads it to surface a re-issue hint to the agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext


TRUNCATED_ATTR = "truncated"
MAX_ROWS_ATTR = "max_rows"


def mark_truncation(df: pd.DataFrame, *, truncated: bool, max_rows: int) -> None:
    """Stamp a DataFrame with row-cap metadata.

    Helper used by per-runner adapters so the truncation contract is in one
    place. ``RunSqlTool`` reads the same attrs to surface a re-issue hint to
    the agent.
    """
    df.attrs[TRUNCATED_ATTR] = truncated
    df.attrs[MAX_ROWS_ATTR] = max_rows


class RowCapRunner(SqlRunner):
    """Decorator that enforces ``max_rows`` on the returned DataFrame.

    Wrapped after the per-runner adapter; runs before any other downstream
    consumer. Safe to stack with ``ReadOnlyGuardRunner``.
    """

    def __init__(self, inner: SqlRunner, *, max_rows: int) -> None:
        if max_rows < 1:
            raise ValueError(f"max_rows must be >= 1 (got {max_rows})")
        self._inner = inner
        self._max_rows = max_rows

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        df = await self._inner.run_sql(args, context)
        if not isinstance(df, pd.DataFrame):
            return df

        already_truncated = bool(df.attrs.get(TRUNCATED_ATTR, False))
        if len(df) > self._max_rows:
            df = df.iloc[: self._max_rows].copy()
            mark_truncation(df, truncated=True, max_rows=self._max_rows)
        elif already_truncated:
            mark_truncation(
                df,
                truncated=True,
                max_rows=int(df.attrs.get(MAX_ROWS_ATTR, self._max_rows)),
            )
        else:
            mark_truncation(df, truncated=False, max_rows=self._max_rows)
        return df
