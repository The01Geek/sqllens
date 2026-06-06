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
from sqllens.runtime import get_effective_settings

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext


TRUNCATED_ATTR = "truncated"
MAX_ROWS_ATTR = "max_rows"


def mark_truncation(df: pd.DataFrame, *, truncated: bool, max_rows: int) -> None:
    """Stamp a DataFrame with row-cap metadata that ``RunSqlTool`` reads."""
    df.attrs[TRUNCATED_ATTR] = truncated
    df.attrs[MAX_ROWS_ATTR] = max_rows


def effective_row_cap(constructor_cap: int) -> int:
    """Narrowest row cap honoured by a per-call request — never wider than the constructor.

    The constructor cap is the operator's ceiling (set on the integration
    runner from ``cfg.database.max_rows``); per-request profiles overlay
    a request-local :class:`~sqllens.runtime.EffectiveSettings` that can
    *narrow* it. Returning ``min(constructor_cap, effective.max_rows)``
    means a profile cannot raise the runaway-loop ceiling the operator
    chose, but it can tighten it for one request. When no effective
    settings are bound (boot warmup, CLI, tests), the constructor cap
    stands.

    Centralizing this avoids three identical copies inside the sqlite,
    mysql, and postgres runners — see ``effective_row_cap`` calls there.
    """
    effective = get_effective_settings()
    if effective is None:
        return constructor_cap
    return min(constructor_cap, effective.max_rows)


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
        cap = effective_row_cap(self._max_rows)
        df = await self._inner.run_sql(args, context)
        already_truncated = bool(df.attrs.get(TRUNCATED_ATTR, False))
        if len(df) > cap:
            df = df.iloc[:cap].copy()
            mark_truncation(df, truncated=True, max_rows=cap)
        elif already_truncated:
            mark_truncation(
                df,
                truncated=True,
                max_rows=int(df.attrs.get(MAX_ROWS_ATTR, cap)),
            )
        else:
            mark_truncation(df, truncated=False, max_rows=cap)
        return df
