# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Node-run checks of the table widget's cut notice and Export CSV warning (#337)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_widget_export_outcomes import FAKES, NODE, _between, _run_js

UI = Path(__file__).resolve().parents[2] / "src" / "sqllens" / "ui"

pytestmark = pytest.mark.skipif(NODE is None, reason="node required for widget behavior")

FORMULA = (
    "Warning: CSV formula protection added apostrophes to some headers or cells; "
    "spreadsheets will treat them as text."
)


def _render(block: dict, filter_text: str = "") -> dict:
    """Run the real ``renderTableBlock`` on fake DOM nodes and report what it drew."""
    html = (UI / "query_results.html").read_text()
    start, end = "function renderTableBlock(", "// ───────────────────────────── chart block"
    assert start in html and end in html, "renderTableBlock extraction markers missing"
    source = start + html.split(start, 1)[1].split(end, 1)[0]
    script = "const host='nocap', clip='none'; const PAGE_SIZE=50;\n" + FAKES
    script += "function flashMeta(){}\n" + source
    script += f"const block={json.dumps(block)};\n"
    script += r'''
      function find(n, cls) {
        const hits = n.className === cls ? [n] : [];
        return hits.concat(n.children.flatMap((c) => find(c, cls)));
      }
      const c = renderTableBlock(block);
      const [noticeHost, barHost] = c.children;
      console.log(JSON.stringify({
        notices: find(c, "cutnotice").map((n) => n.textContent),
        noticeFirst: find(noticeHost, "cutnotice").length === find(c, "cutnotice").length,
        barAfterNotice: find(barHost, "bar").length === 1,
        meta: find(barHost, "meta").map((n) => n.textContent),
      }));
    '''
    return _run_js(script)


def _csv_warning(state: dict) -> str:
    html = (UI / "query_results.html").read_text()
    source = _between(html, "  // ── CSV export (per-block)", "  // ── Bar (filter input")
    script = "const host='nocap', clip='none';\n" + f"const state={json.dumps(state)};\n"
    script += "const sortedRows=x=>x;\n"
    script += "const filteredRows=()=>state.rows.filter(r=>r[0]!=='skip');\n"
    script += "const render=()=>{};\n" + FAKES + "const gridHost=node(); let metaSpan=node();\n"
    script += source + "console.log(JSON.stringify(buildCsvExport().warning));\n"
    return _run_js(script)


def _block(rows: int, truncated: int = 0, row_cap: int | None = None) -> dict:
    block = {
        "columns": ["v"],
        "rows": [[str(i)] for i in range(rows)],
        "row_count": rows,
        "truncated": truncated,
    }
    if row_cap is not None:
        block["row_cap"] = row_cap
    return block


DISPLAY = (
    "Showing the first 183 of 300 rows. "
    "The other 117 rows were left out to fit the display size limit."
)


def test_display_cut_notice_above_bar_and_count_without_suffix() -> None:
    out = _render(_block(183, truncated=117))
    assert out["notices"] == [DISPLAY]
    assert out["noticeFirst"] and out["barAfterNotice"]
    assert out["meta"][0] == "showing 183 of 183"


def test_row_cap_notice_uses_cap_in_effect_with_commas() -> None:
    out = _render(_block(3, row_cap=12500))
    assert out["notices"] == ["The query stopped at the 12,500-row limit; more rows exist."]


def test_both_limits_one_line_display_sentence_first() -> None:
    out = _render(_block(1500, truncated=8500, row_cap=10000))
    assert out["notices"] == [
        "Showing the first 1,500 of 10,000 rows. The other 8,500 rows were left out to "
        "fit the display size limit. The query stopped at the 10,000-row limit; more rows exist."
    ]


def test_uncut_table_has_no_notice() -> None:
    out = _render(_block(5))
    assert out["notices"] == []
    assert out["meta"][0] == "showing 5 of 5"


def test_csv_warning_counts_exported_rows_after_filter() -> None:
    state = {
        "columns": ["v"],
        "rows": [["a"], ["skip"], ["b"]],
        "rowCount": 3,
        "truncated": 1297,
        "rowCap": 0,
    }
    assert _csv_warning(state) == "Warning: this CSV holds only the 2 rows shown, not all 1,300."


def test_csv_warning_row_cap_and_formula_warning_together() -> None:
    state = {"columns": ["v"], "rows": [["=1+1"]], "rowCount": 1, "truncated": 0, "rowCap": 5000}
    assert _csv_warning(state) == (
        "Warning: this CSV holds only the 1 rows shown. The query stopped at the "
        "5,000-row limit; more rows exist. " + FORMULA
    )


def test_csv_uncut_table_keeps_only_formula_warning() -> None:
    state = {"columns": ["v"], "rows": [["=1+1"]], "rowCount": 1, "truncated": 0, "rowCap": 0}
    assert _csv_warning(state) == FORMULA
