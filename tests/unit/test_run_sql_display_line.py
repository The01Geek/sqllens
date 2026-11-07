# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The ``run_sql`` display line: the agent is told when the user's table is cut (#337)."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.components import UiComponent
from sqllens.agent.markers import row_cap_of
from sqllens.agent.tools.run_sql import RunSqlTool
from sqllens.safety.limits import mark_truncation
from sqllens.tools._format import _MAX_TABLE_PAYLOAD_BYTES, components_to_blocks


class _Ctx:
    request_context = None
    user = None
    conversation = None
    output_directory = "/tmp"


class _StaticRunner(SqlRunner):
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def run_sql(self, args: RunSqlToolArgs, context: Any) -> pd.DataFrame:
        return self._df.copy()


class _NoopFileSystem:
    async def write_file(
        self, filename: str, content: str, context: Any, overwrite: bool = False
    ) -> None:
        return None


def _wide_df(n_rows: int, *, capped_at: int | None = None) -> pd.DataFrame:
    df = pd.DataFrame({"c": [f"{i}-" + "x" * 600 for i in range(n_rows)]})
    mark_truncation(df, truncated=capped_at is not None, max_rows=capped_at or 10_000)
    return df


async def _run(df: pd.DataFrame, sql: str = "SELECT c FROM t") -> Any:
    tool = RunSqlTool(
        sql_runner=_StaticRunner(df), file_system=_NoopFileSystem(), dialect="sqlite"
    )
    return await tool.execute(_Ctx(), RunSqlToolArgs(sql=sql))


def _formatter_table(out: Any) -> dict:
    _md, _err, blocks, _qi, _mi = components_to_blocks([out.ui_component])
    return next(b for b in blocks if b["type"] == "table")


@pytest.mark.asyncio
async def test_display_cut_line_matches_formatter_row_count() -> None:
    out = await _run(_wide_df(300))
    n = _formatter_table(out)["row_count"]
    assert 0 < n < 300
    assert out.result_for_llm.endswith(
        f"The query returned 300 rows; the user's table shows only the first {n} "
        "of them. Say so in your answer."
    )


@pytest.mark.asyncio
async def test_row_cap_marker_set_on_component_and_counted_in_budget() -> None:
    out = await _run(_wide_df(300, capped_at=300))
    assert row_cap_of(out.ui_component.rich_component) == 300
    table = _formatter_table(out)
    assert table["row_cap"] == 300
    n = table["row_count"]
    result = out.result_for_llm
    assert "Result truncated at 300 rows" in result
    assert f"the user's table shows only the first {n} of them" in result


@pytest.mark.asyncio
async def test_uncut_result_gets_no_display_line() -> None:
    df = pd.DataFrame({"n": list(range(600))})
    mark_truncation(df, truncated=False, max_rows=10_000)
    out = await _run(df, "SELECT n FROM t")
    assert "user's table" not in out.result_for_llm
    assert row_cap_of(out.ui_component.rich_component) is None


@pytest.mark.asyncio
async def test_schema_lookup_gets_no_display_line() -> None:
    out = await _run(_wide_df(300), "SELECT name FROM sqlite_master")
    assert "user's table" not in out.result_for_llm


@pytest.mark.asyncio
async def test_unparseable_sql_is_treated_as_data_query() -> None:
    out = await _run(_wide_df(300), "SELECT c FROM t WHERE (")
    assert "the user's table shows only the first" in out.result_for_llm


@pytest.mark.asyncio
async def test_headers_too_large_line() -> None:
    huge = "h" * (_MAX_TABLE_PAYLOAD_BYTES + 50)
    df = pd.DataFrame({huge: list(range(2000))})
    mark_truncation(df, truncated=False, max_rows=10_000)
    out = await _run(df)
    assert out.result_for_llm.endswith(
        "The query returned 2,000 rows, which are too large to show the user as a table."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["is_introspection_query", "_compute_table_payload"])
async def test_raising_display_work_keeps_todays_result(
    monkeypatch, caplog, target: str
) -> None:
    import sqllens._table_payload as tp

    baseline = await _run(_wide_df(300))

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(tp, target, boom)
    with caplog.at_level(logging.WARNING, logger="sqllens._table_payload"):
        out = await _run(_wide_df(300))
    assert out.success is True
    assert "user's table" not in out.result_for_llm
    expected = baseline.result_for_llm.split("\n\nThe query returned")[0]
    # The CSV file name is random per call; compare the rest of the result.
    assert out.result_for_llm.rsplit("query_results_", 1)[0] == expected.rsplit(
        "query_results_", 1
    )[0]
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert isinstance(out.ui_component, UiComponent)


@pytest.mark.parametrize("bad_cap", [0, -1, True, False, "300", 2.5, None])
def test_row_cap_of_rejects_malformed_values(bad_cap: object) -> None:
    from types import SimpleNamespace

    assert row_cap_of(SimpleNamespace(data={"row_cap": bad_cap})) is None


def test_row_cap_of_ignores_non_dict_data() -> None:
    from types import SimpleNamespace

    assert row_cap_of(SimpleNamespace(data=None)) is None
    assert row_cap_of(SimpleNamespace(data={"row_cap": 7})) == 7
