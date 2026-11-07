# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Node-run checks of each widget export outcome (issue 340).

The table widget's ``exportCsv`` and memory admin's ``offerDownload`` are
extracted from the packaged HTML between their source markers and run against
fakes of each host answer: no ``downloadFile`` capability, a download that
resolves (empty or ``isError: true``), a download that throws (SDK timeout or
another error), and clipboards that succeed or fail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "sqllens" / "ui"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node required for widget behavior")

TIMEOUT_ERROR = {"code": -32001, "message": "MCP error -32001: Request timed out"}
OTHER_ERROR = {"message": "connection lost"}

# (case id, host answer, clipboard route)
#   host answer: "nocap" | {"resolve": value} | {"throw": error}
#   clipboard route: "async" | "legacy" | "none"
CASES = {
    "nocap-clipboard": ("nocap", "async"),
    "nocap-legacy": ("nocap", "legacy"),
    "empty-result": ({"resolve": {}}, "async"),
    "undefined-result": ({"resolve": None}, "async"),
    "iserror": ({"resolve": {"isError": True}}, "async"),
    "timeout": ({"throw": TIMEOUT_ERROR}, "async"),
    "other-error": ({"throw": OTHER_ERROR}, "legacy"),
    "nocap-overlay": ("nocap", "none"),
    "timeout-overlay": ({"throw": TIMEOUT_ERROR}, "none"),
}

FAKES = r'''
  const outputs = [], downloads = [], timers = [];
  const setTimeout = (fn, delay) => timers.push(delay);
  function node() { return {children:[],_text:"",style:{},classList:{add(){},remove(){}},
    get textContent(){return this._text+this.children.map(x=>x.textContent).join("")},
    set textContent(v){this._text=v; this.children=[]},
    append(...xs){xs.forEach(x=>this.appendChild(x))},
    appendChild(x){x.parent=this; this.children.push(x)},
    setAttribute(){},addEventListener(){},focus(){},select(){},setSelectionRange(){},
    remove(){if(this.parent) this.parent.children=this.parent.children.filter(x=>x!==this)}}; }
  function textareas(n) {
    return n.value === undefined ? n.children.flatMap(textareas) : [n.value]; }
  const document = {body: node(), createElement: node, execCommand() {
    if (clip !== "legacy") return false;
    outputs.push(textareas(document.body)[0]); return true; }};
  const navigator = {clipboard: {async writeText(text) {
    if (clip !== "async") throw Error("clipboard disabled in harness"); outputs.push(text); }}};
  const app = {
    getHostCapabilities: () => (host === "nocap" ? {} : {downloadFile: {}}),
    async downloadFile(params, options) {
      downloads.push({params, options});
      if ("throw" in host) throw Object.assign(new Error(host.throw.message), host.throw);
      return host.resolve === null ? undefined : host.resolve;
    },
  };
'''


def _run_js(source: str) -> dict:
    result = subprocess.run([NODE, "-"], input=source, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _between(html: str, start: str, end: str) -> str:
    assert start in html and end in html, f"extraction marker missing: {start!r} / {end!r}"
    return html.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]


def _run_table(case: str) -> dict:
    html = (UI / "query_results.html").read_text()
    source = _between(html, "  // ── CSV export (per-block)", "  // ── Bar (filter input")
    host, clip = CASES[case]
    # "=1+1" forces formula protection, so every route must carry the warning tail.
    state = {"columns": ["v"], "rows": [["=1+1"]]}
    script = f"const host={json.dumps(host)}, clip={json.dumps(clip)};\n"
    script += f"const state={json.dumps(state)};\n"
    script += "const sortedRows=x=>x, filteredRows=()=>state.rows, render=()=>{};\n"
    script += FAKES + "const gridHost=node(); let metaSpan=node();\n" + source
    script += r'''
      (async () => { await exportCsv();
        console.log(JSON.stringify({meta: metaSpan.textContent, overlay: gridHost.textContent,
          overlayText: textareas(gridHost)[0] || null, copied: outputs, downloads, timers})); })();
    '''
    return _run_js(script)


def _run_memory(case: str, fmt: str = "csv") -> dict:
    html = (UI / "memory_admin.html").read_text()
    source = _between(html, "// ── Export download cascade", "// ── End export download cascade")
    host, clip = CASES[case]
    script = f"const host={json.dumps(host)}, clip={json.dumps(clip)};\n" + FAKES
    script += r'''
      const banners = [];
      const showOk = (m) => banners.push({kind: "ok", text: m});
      const showError = (t, m) => banners.push({kind: "error", text: t + " | " + m});
      const modalBody = node(), rowModal = node();
    ''' + source
    script += f"const fmt={json.dumps(fmt)};\n"
    script += r'''
      (async () => {
        await offerDownload("memory-export." + fmt,
          fmt === "csv" ? "text/csv" : "application/json", "PAYLOAD", ["store is empty"]);
        console.log(JSON.stringify({banners, overlay: modalBody.textContent,
          overlayText: textareas(modalBody)[0] || null, copied: outputs, downloads})); })();
    '''
    return _run_js(script)


TABLE_TAIL = " Warning: CSV formula protection"
MEMORY_TAIL = " Warnings: store is empty"


@pytest.mark.parametrize("case", ["nocap-clipboard", "nocap-legacy"])
def test_no_capability_names_the_reason_for_the_clipboard_copy(case: str) -> None:
    table = _run_table(case)
    assert table["meta"].startswith(
        "This host can't save files — CSV copied to clipboard" + TABLE_TAIL)
    assert table["copied"] == ["v\r\n'=1+1"]
    assert table["downloads"] == []
    memory = _run_memory(case)
    assert memory["banners"] == [{"kind": "ok", "text":
        "This host can't save files — Export copied to clipboard" + MEMORY_TAIL}]
    assert memory["copied"] == ["PAYLOAD"]


@pytest.mark.parametrize("case", ["empty-result", "undefined-result"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_resolved_download_reports_the_saved_file_and_copies_nothing(case: str, fmt: str) -> None:
    table = _run_table(case)
    assert table["meta"].startswith("Downloaded query_results.csv ✓" + TABLE_TAIL)
    assert table["copied"] == [] and table["overlayText"] is None
    memory = _run_memory(case, fmt)
    assert memory["banners"] == [{"kind": "ok", "text":
        f"Downloaded memory-export.{fmt} ✓" + MEMORY_TAIL}]
    assert memory["copied"] == [] and memory["overlayText"] is None


def test_iserror_download_stops_without_copying_or_claiming_success() -> None:
    table = _run_table("iserror")
    assert table["meta"].startswith("Download cancelled or blocked by the host." + TABLE_TAIL)
    assert "Downloaded" not in table["meta"]
    assert table["copied"] == [] and table["overlayText"] is None
    memory = _run_memory("iserror")
    assert [b["kind"] for b in memory["banners"]] == ["error"]
    banner = memory["banners"][0]["text"]
    assert "Download cancelled or blocked by the host." + MEMORY_TAIL in banner
    assert "Downloaded" not in banner
    assert memory["copied"] == [] and memory["overlayText"] is None


@pytest.mark.parametrize(("case", "reason"), [("timeout", "timed out"),
                                              ("other-error", "connection lost")])
def test_thrown_download_names_its_reason_on_the_clipboard_copy(case: str, reason: str) -> None:
    table = _run_table(case)
    assert table["meta"].startswith(
        f"Download failed ({reason}) — CSV copied to clipboard" + TABLE_TAIL)
    assert len(table["copied"]) == 1
    memory = _run_memory(case)
    assert memory["banners"] == [{"kind": "ok", "text":
        f"Download failed ({reason}) — Export copied to clipboard" + MEMORY_TAIL}]
    assert memory["copied"] == ["PAYLOAD"]


@pytest.mark.parametrize(("case", "reason"), [("nocap-overlay", "This host can't save files"),
                                              ("timeout-overlay", "Download failed (timed out)")])
def test_overlay_hint_opens_with_the_fallback_reason(case: str, reason: str) -> None:
    hint = f"{reason} and the clipboard is blocked. Select all and copy:"
    table = _run_table(case)
    assert table["overlay"].startswith(hint)
    assert "Host blocked the file download" not in table["overlay"]
    assert table["overlayText"] is not None and table["copied"] == []
    memory = _run_memory(case)
    assert memory["overlay"].startswith(hint)
    assert "Host blocked the file download" not in memory["overlay"]
    assert memory["overlayText"] == "PAYLOAD" and memory["copied"] == []
    assert MEMORY_TAIL.strip() in memory["overlay"]


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_download_request_uses_file_uri_and_ten_minute_timeout(fmt: str) -> None:
    (table_call,) = _run_table("empty-result")["downloads"]
    assert table_call["params"]["contents"][0]["resource"]["uri"] == "file:///query_results.csv"
    assert table_call["options"] == {"timeout": 600000}
    (memory_call,) = _run_memory("empty-result", fmt)["downloads"]
    assert memory_call["params"]["contents"][0]["resource"]["uri"] == f"file:///memory-export.{fmt}"
    assert memory_call["options"] == {"timeout": 600000}
