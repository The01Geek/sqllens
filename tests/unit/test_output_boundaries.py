# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Data remains data in Markdown, spreadsheet exports and host theming."""

import csv
import io
import json
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqllens.memory.admin import export_memories
from sqllens.memory.exporter import export_bundle
from sqllens.memory.io import parse_csv
from sqllens.memory.schema import MemoryBundle, SqlPair, SqlPairsBlock
from sqllens.tools._format import _table_block_to_markdown
from sqllens.tools.query_database import _append_sql_block

from .test_ui_widget_markdown import render

UI = Path(__file__).resolve().parents[2] / "src/sqllens/ui"
NODE = shutil.which("node")
VALUES = ["=1+1", "+1", "-1", "@x", "\tx", "\rx", "\nx", "", "\t", "'safe", 'a,"b', "a\rb"]


@pytest.mark.parametrize("row_count,column_count", [(2, 2), (1, 5)])
def test_table_markdown_escapes_headers_and_cells_without_mutating_data(
    row_count: int, column_count: int,
) -> None:
    value = "a|b\r\n[link](x)<b>*x*`code` &amp;"
    block = {"columns": [value] * column_count,
             "column_labels": [value] * column_count,
             "rows": [[value] * column_count for _ in range(row_count)]}
    original = deepcopy(block)
    result = _table_block_to_markdown(block)
    expected = r"a\|b  \[link\](x)\<b\>\*x\*\`code\` \&amp;"
    assert result.splitlines()[0] == "| " + " | ".join([expected] * column_count) + " |"
    assert result.splitlines()[2:] == [result.splitlines()[0]] * row_count
    assert block == original


@pytest.mark.skipif(NODE is None, reason="node required for Markdown rendering")
@pytest.mark.parametrize("run", [3, 4, 8])
def test_executed_sql_cannot_close_its_fence(run: int) -> None:
    sql = "SELECT 'text\n" + "`" * run + "\n**literal**\n'"
    info = {"sql": sql}
    result = _append_sql_block("answer", info)
    fence = "`" * (run + 1)
    assert f"{fence}sql\n{sql}\n{fence}" in result
    assert "<strong>literal</strong>" not in render(result)
    assert render(result).count("<pre>") == 1
    assert info == {"sql": sql}


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "\n"])
def test_mcp_and_cli_csv_share_formula_protection_and_json_is_unchanged(prefix: str) -> None:
    value = prefix + "synthetic"
    bundle = MemoryBundle(sql_pairs=SqlPairsBlock(pairs=[SqlPair(question=value, sql=value)]))
    store = SimpleNamespace(iter_all=lambda: bundle, last_skipped_rows=0)
    exported = export_memories(store, "csv")
    assert exported["data"] == export_bundle(store, "csv").text
    assert list(csv.reader(io.StringIO(exported["data"]))) == [
        ["question", "sql"], ["'" + value, "'" + value],
    ]
    assert exported["lossy"] is False and exported["warnings"] == []
    imported = parse_csv(exported["data"])
    again = export_memories(SimpleNamespace(iter_all=lambda: imported, last_skipped_rows=0), "csv")
    assert again["data"] == exported["data"]
    assert json.loads(export_memories(store, "json")["data"])["sql_pairs"] == [
        {"question": value, "sql": value},
    ]


def _run_js(source: str) -> object:
    result = subprocess.run([NODE, "-"], input=source, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node required for widget behavior")
def test_widget_csv_defangs_both_headers_and_rows_before_quoting() -> None:
    html = (UI / "query_results.html").read_text()
    functions = re.search(
        r"  function csvEscape\(v\) \{.*?\n  \}\n\n  function buildCsvText\(\) \{.*?\n  \}",
        html, re.S,
    )
    assert functions
    source = f"const values = {json.dumps(VALUES)}; const state = {{columns:values}};\n"
    source += "const filteredRows=()=>[values]; const sortedRows=x=>x;\n"
    source += functions.group() + "\nconsole.log(JSON.stringify(buildCsvText()));"
    output = _run_js(source)
    expected = ["'" + value if value and value.strip() and value[0] in "=+-@\t\r\n" else value
                for value in VALUES]
    assert list(csv.reader(io.StringIO(output, newline=""))) == [expected, expected]


@pytest.mark.skipif(NODE is None, reason="node required for widget behavior")
@pytest.mark.parametrize("widget", ["query_results", "config_admin", "memory_admin"])
def test_host_theme_only_sets_string_color_custom_properties(widget: str) -> None:
    html = (UI / f"{widget}.html").read_text()
    function = re.search(r"function applyHostContext\(ctx\) \{.*?\n\}", html, re.S)
    assert function
    source = """
      const calls=[];
      const document={documentElement:{dataset:{},setAttribute(){},
        style:{setProperty(k,v){calls.push([k,v]);}}}};
      const widgetState={theme:'dark'},state={theme:'dark'},chartRegistry=[];
    """ + function.group() + """
      applyHostContext({theme:'dark',styles:{variables:{
        '--color-text-primary':'#123456', '--color-background-primary':'white',
        'background-image':'url(https://example.invalid/pixel)',
        'display':'none','--unrelated':'red','--color-number':42,
        '--color-null':null,'--color-object':{},'--color-array':[]
      }}});
      console.log(JSON.stringify(calls));
    """
    assert _run_js(source) == [
        ["--color-text-primary", "#123456"], ["--color-background-primary", "white"],
    ]
