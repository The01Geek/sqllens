# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-cap enforcement for SqlRunner implementations.

The per-runner adapters stream rows via ``fetchmany(max_rows + 1)`` and stamp
truncation metadata on the returned DataFrame. ``RowCapRunner`` is a secondary
guard that re-applies the cap on the way back out — so a future runner that
forgets to stream still cannot return more than ``max_rows`` rows downstream.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

import pandas as pd

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext


TRUNCATED_ATTR = "truncated"
MAX_ROWS_ATTR = "max_rows"


def mark_truncation(df: pd.DataFrame, *, truncated: bool, max_rows: int) -> None:
    """Stamp a DataFrame with row-cap metadata that ``RunSqlTool`` reads."""
    df.attrs[TRUNCATED_ATTR] = truncated
    df.attrs[MAX_ROWS_ATTR] = max_rows


def rows_to_capped_df(rows: Iterable[Mapping], max_rows: int) -> pd.DataFrame:
    """Trim ``rows`` to ``max_rows``, build a DataFrame, stamp truncation attrs.

    Callers pass the result of ``cursor.fetchmany(max_rows + 1)`` — the +1
    sentinel lets us detect truncation without a second round trip.
    """
    rows = list(rows)
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    if not rows:
        df = pd.DataFrame()
    else:
        df = pd.DataFrame([dict(row) for row in rows])
    mark_truncation(df, truncated=truncated, max_rows=max_rows)
    return df


class RowCapRunner(SqlRunner):
    """Decorator that enforces ``max_rows`` on the returned DataFrame."""

    def __init__(self, inner: SqlRunner, *, max_rows: int) -> None:
        if max_rows < 1:
            raise ValueError(f"max_rows must be >= 1 (got {max_rows})")
        self._inner = inner
        self._max_rows = max_rows

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        df = await self._inner.run_sql(args, context)
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
