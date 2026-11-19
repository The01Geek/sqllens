# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Render the memory-admin widget in headless Chromium against a stub host (#414).

Skipped when no Chromium-family binary is found; set ``SQLLENS_TEST_CHROMIUM``
to a binary path to run it.
"""

from __future__ import annotations

import html as html_module
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sqllens.ui import load_widget_html

from .test_access_rules_widget_render import CHROMIUM

pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="headless Chromium not available")

WIDE = [f"group-with-a-rather-long-name-{i}" for i in range(6)]
XSS = "<img src=x onerror=alert(1)>"


def _mem(i: int, groups: Any, question: str) -> dict[str, Any]:
    return {
        "memory_id": f"{i:08d}-aaaa-bbbb-cccc-00000000000{i}",
        "memory_type": "tool_usage",
        "question": question,
        "sql": "SELECT 1",
        "timestamp": f"2026-05-0{i}T00:00:00Z",
        "hit_count": i,
        "groups": groups,
    }


MEMORIES = [
    _mem(1, ["finance", "sales"], "two names"),
    _mem(2, WIDE, "wide chips"),
    _mem(3, ["a, b"], "comma name"),
    _mem(4, ["untagged"], "named untagged"),
    _mem(5, ["unreadable"], "named unreadable"),
    _mem(6, [XSS], "html name"),
    _mem(7, [], "empty list"),
    _mem(8, None, "null groups"),
]
FILLER = [_mem(9, ["zz-filler"], f"filler {i}") for i in range(52)]
OTHER = [_mem(9, ["other"], "after reload")]

# Stub host routing by tool name. `window.__lists` holds the successive
# list_memories payloads so a step can reload Browse and see different data.
_STUB = """
var App = class { connect() { return Promise.resolve(); }
  getHostContext() { return null; }
  callServerTool(req) {
    window.__calls = (window.__calls || []).concat([req.name]);
    let body;
    if (req.name === "list_memories") {
      const mems = window.__lists.length > 1 ? window.__lists.shift() : window.__lists[0];
      // Like the server: newest first, at most `limit` rows, `total` counts all.
      body = {memories: mems.slice(0, req.arguments.limit), total: mems.length};
    } else if (req.name === "get_memory_stats") {
      body = {tool_usage_count: 0, text_count: 0, total_hits_last_30d: 0, top_hit_memories: []};
    } else if (req.name === "get_memory") {
      const m = window.__all.find((x) => x.memory_id === req.arguments.memory_id);
      body = m;
    } else { body = {}; }
    return Promise.resolve({isError: false, content: [{type: "text", text: JSON.stringify(body)}]});
  } };
"""

_PROBE = """
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const q = (s) => document.querySelector(s);
const rowIds = () => [...document.querySelectorAll("#browseBody tbody tr")]
  .map((tr) => tr.children[3].textContent);
const pagePos = () => q("#browseBody .pager .meta").textContent;
const menuLabels = () => [...document.querySelectorAll("#groupMenu label")];
const clickChoice = (text) => {
  const lab = menuLabels().find((l) => l.children[1].textContent === text
    && (text === "untagged" || text === "⚠ unreadable"
        ? !l.children[1].classList.contains("chip") : l.children[1].classList.contains("chip")));
  lab.querySelector("input").click();
};
const clickName = (text) => {
  const lab = menuLabels().find((l) => l.children[1].classList.contains("chip")
    && l.children[1].textContent === text);
  lab.querySelector("input").click();
};
(async () => {
  for (let i = 0; i < 100 && !document.querySelector("#browseBody tbody tr"); i++) await sleep(20);
  const out = {};
  out.headers = [...document.querySelectorAll("#browseBody th")].map((t) => t.textContent);
  const cells = [...document.querySelectorAll("#browseBody tbody tr")].map((tr) => {
    const g = tr.children[2];
    const box = g.firstElementChild;
    const cs = getComputedStyle(box);
    return {
      question: tr.children[3].textContent,
      classes: [...tr.children].map((td) => td.className),
      chips: [...g.querySelectorAll(".chip")].map((c) => c.textContent),
      text: g.textContent,
      title: (box.title || (box.firstElementChild && box.firstElementChild.title) || ""),
      stateClass: box.firstElementChild ? box.firstElementChild.className : "",
      color: box.firstElementChild ? getComputedStyle(box.firstElementChild).color : "",
      width: box.getBoundingClientRect().width,
      overflow: cs.overflow, textOverflow: cs.textOverflow,
      hitsAlign: getComputedStyle(tr.children[4]).textAlign,
    };
  });
  out.cells = cells;
  out.imgCount = document.querySelectorAll("img").length;
  const probeColor = (cls) => {
    const s = document.createElement("span");
    s.style.color = cls;
    document.body.append(s);
    const c = getComputedStyle(s).color;
    s.remove();
    return c;
  };
  out.mutedColor = probeColor("var(--muted)");
  out.errColor = probeColor("var(--err-fg)");
  out.btnInitial = q("#groupBtn").textContent;
  // toolbar order
  out.toolbar = [...q("#pane-browse .bar").querySelectorAll(":scope > *")]
    .map((e) => e.id || (e.querySelector("button") && e.querySelector("button").id));
  out.menu = menuLabels().map((l) => ({
    text: l.children[1].textContent,
    chip: l.children[1].classList.contains("chip"),
    color: getComputedStyle(l.children[1]).color,
  }));
  // Search does not match group names.
  const f = q("#filter");
  f.value = "finance"; f.dispatchEvent(new Event("input"));
  out.searchFinance = rowIds();
  f.value = ""; f.dispatchEvent(new Event("input"));
  // Page reset: go to page 2, then change the selection.
  [...document.querySelectorAll("#browseBody .pager button")][1].click();
  out.pageBefore = pagePos();
  // zz-filler alone still spans two pages, so landing on page 1 is the reset.
  clickName("zz-filler");
  out.pageAfterSelect = pagePos();
  clickName("zz-filler");
  clickChoice("untagged"); clickName("sales");
  out.untaggedSales = rowIds();
  out.btnTwo = q("#groupBtn").textContent;
  clickChoice("untagged"); clickName("sales");
  clickChoice("⚠ unreadable");
  out.unreadableOnly = rowIds();
  clickChoice("⚠ unreadable");
  clickName("untagged");
  out.nameUntagged = rowIds();
  clickName("untagged");
  clickName("unreadable");
  out.nameUnreadable = rowIds();
  clickName("unreadable");
  out.noneSelected = rowIds();
  // Combined with search: group finance + search.
  clickName("finance");
  out.financeOnly = rowIds();
  f.value = "two"; f.dispatchEvent(new Event("input"));
  out.groupAndSearch = rowIds();
  f.value = "comma"; f.dispatchEvent(new Event("input"));
  out.groupAndSearchNone = rowIds();
  f.value = ""; f.dispatchEvent(new Event("input"));
  // clear link
  q("#groupClear").click();
  out.afterClear = rowIds();
  out.btnAfterClear = q("#groupBtn").textContent;
  // Detail view for each state.
  out.detail = {};
  for (const [idx, key] of [[0, "names"], [6, "empty"], [7, "null"], [1, "wide"]]) {
    document.querySelectorAll("#browseBody tbody tr")[idx].click();
    await sleep(50);
    const row = q("#detailGroups");
    const v = row.querySelector(".v").firstElementChild;
    out.detail[key] = {
      key: row.querySelector(".k").textContent,
      chips: [...row.querySelectorAll(".chip")].map((c) => c.textContent),
      text: row.querySelector(".v").textContent,
      title: v.title || (v.firstElementChild && v.firstElementChild.title) || "",
      capped: v.classList.contains("groups-cap"),
    };
    q("#closeModal").click();
  }
  // Keep a selected name across a reload that drops it.
  clickName("sales");
  q("#refresh").click();
  await sleep(100);
  out.reloadMenu = menuLabels().map((l) => [l.children[1].textContent,
    l.querySelector("input").checked]);
  out.reloadBody = q("#browseBody").textContent;
  out.reloadBtn = q("#groupBtn").textContent;
  const pre = document.createElement("pre");
  pre.id = "probe";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
"""


def _page(lists: list[list[dict[str, Any]]]) -> str:
    html = load_widget_html("memory_admin.html")
    match = re.search(r"var App = \w+;", html)
    assert match
    data = (
        "window.__lists = " + json.dumps(lists) + ";"
        "window.__all = " + json.dumps([m for ms in lists for m in ms]) + ";"
    )
    page = html[: match.start()] + data + _STUB + html[match.end():]
    return page.replace("</script>\n</body>", _PROBE + "</script>\n</body>")


def _render(tmp_path: Path, page: str, width: int) -> dict[str, Any]:
    path = tmp_path / "widget.html"
    path.write_text(page, encoding="utf-8")
    proc = subprocess.run(
        [CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu", f"--window-size={width},900",
         "--virtual-time-budget=5000", "--dump-dom", path.as_uri()],
        capture_output=True, text=True, timeout=60, check=False,
    )
    match = re.search(r'<pre id="probe">(.*?)</pre>', proc.stdout, re.S)
    assert match, proc.stderr[-2000:]
    return json.loads(html_module.unescape(match.group(1)))


UNTAGGED_TITLE = "No groups: every rule set can read it."
UNREADABLE_TITLE = (
    "Marked tagged but its group list is unreadable. With a rule-set file, no rule set "
    "retrieves it."
)


@pytest.fixture(scope="module")
def renders(tmp_path_factory: pytest.TempPathFactory) -> dict[int, dict[str, Any]]:
    page = _page([MEMORIES + FILLER, OTHER])
    return {w: _render(tmp_path_factory.mktemp(f"w{w}"), page, w) for w in (700, 1400)}


def _by_q(out: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {i + 1: out["cells"][i] for i in range(8)}


def test_header_order_and_cell_classes(renders: dict[int, dict[str, Any]]) -> None:
    out = renders[1400]
    assert out["headers"] == ["id", "type", "groups", "preview", "hits", "created"]
    for c in out["cells"]:
        assert c["classes"] == ["", "", "groups", "trunc", "num", ""]
        assert c["hitsAlign"] == "right"


def test_cell_states(renders: dict[int, dict[str, Any]]) -> None:
    cells = _by_q(renders[1400])
    assert cells[1]["chips"] == ["finance", "sales"]
    assert cells[1]["title"] == "finance\nsales"
    assert cells[3]["chips"] == ["a, b"]
    assert cells[4]["chips"] == ["untagged"] and cells[5]["chips"] == ["unreadable"]
    assert cells[6]["chips"] == [XSS]
    assert cells[2]["title"] == "\n".join(WIDE)
    out = renders[1400]
    assert cells[7]["chips"] == [] and cells[7]["text"] == "untagged"
    assert cells[7]["title"] == UNTAGGED_TITLE and cells[7]["color"] == out["mutedColor"]
    assert cells[8]["chips"] == [] and cells[8]["text"] == "⚠ unreadable"
    assert cells[8]["title"] == UNREADABLE_TITLE and cells[8]["color"] == out["errColor"]
    assert out["mutedColor"] != out["errColor"]


def test_html_name_is_text_only(renders: dict[int, dict[str, Any]]) -> None:
    assert renders[1400]["imgCount"] == 0


@pytest.mark.parametrize("width", [700, 1400])
def test_wide_groups_cell_is_capped(renders: dict[int, dict[str, Any]], width: int) -> None:
    wide = _by_q(renders[width])[2]
    assert wide["width"] <= 220
    assert wide["overflow"] == "hidden" and wide["textOverflow"] == "ellipsis"


def test_detail_view_groups_row(renders: dict[int, dict[str, Any]]) -> None:
    d = renders[1400]["detail"]
    assert d["names"]["key"] == "groups" and d["names"]["chips"] == ["finance", "sales"]
    assert d["names"]["title"] == "finance\nsales"
    assert d["empty"]["text"] == "untagged" and d["empty"]["title"] == UNTAGGED_TITLE
    assert d["null"]["text"] == "⚠ unreadable" and d["null"]["title"] == UNREADABLE_TITLE
    assert d["wide"]["chips"] == WIDE and not d["wide"]["capped"]


def test_checklist_choices_and_labels(renders: dict[int, dict[str, Any]]) -> None:
    out = renders[1400]
    assert out["btnInitial"] == "groups ▾"
    bar = out["toolbar"]
    assert bar.index("typeFilter") < bar.index("groupBtn") < bar.index("refresh")
    names = ["finance", "sales", *WIDE, "a, b", "untagged", "unreadable", XSS, "zz-filler"]
    menu = out["menu"]
    assert [m["text"] for m in menu[:2]] == ["untagged", "⚠ unreadable"]
    assert not menu[0]["chip"] and not menu[1]["chip"]
    assert menu[0]["color"] == out["mutedColor"] and menu[1]["color"] == out["errColor"]
    assert all(m["chip"] for m in menu[2:])
    assert sorted(m["text"] for m in menu[2:]) == sorted(names)
    assert len(menu) == 2 + len(names)


def test_filter_results(renders: dict[int, dict[str, Any]]) -> None:
    out = renders[1400]
    assert out["untaggedSales"] == ["two names", "empty list"]
    assert out["btnTwo"] == "groups: 2 selected ▾"
    assert out["unreadableOnly"] == ["null groups"]
    assert out["nameUntagged"] == ["named untagged"]
    assert out["nameUnreadable"] == ["named unreadable"]
    assert len(out["noneSelected"]) == 50
    assert out["financeOnly"] == ["two names"]
    assert out["groupAndSearch"] == ["two names"]
    assert out["groupAndSearchNone"] == []
    assert len(out["afterClear"]) == 50 and out["btnAfterClear"] == "groups ▾"


def test_selection_change_returns_to_page_one(renders: dict[int, dict[str, Any]]) -> None:
    out = renders[1400]
    assert out["pageBefore"] == "page 2 / 2"
    assert out["pageAfterSelect"] == "page 1 / 2"


def test_search_does_not_match_group_names(renders: dict[int, dict[str, Any]]) -> None:
    assert renders[1400]["searchFinance"] == []


def test_selected_name_kept_after_reload(renders: dict[int, dict[str, Any]]) -> None:
    out = renders[1400]
    assert ["sales", True] in out["reloadMenu"]
    assert ["other", False] in out["reloadMenu"]
    assert "No memories match the current filter." in out["reloadBody"]
    assert out["reloadBtn"] == "groups: 1 selected ▾"


_WINDOW_PROBE = """
(async () => {
  for (let i = 0; i < 100 && !document.querySelector("#browseBody tbody tr"); i++)
    await new Promise((r) => setTimeout(r, 20));
  const out = {};
  out.menu = [...document.querySelectorAll("#groupMenu label")]
    .map((l) => l.children[1].textContent);
  out.meta = document.querySelector("#browseMeta").textContent;
  const pre = document.createElement("pre");
  pre.id = "probe";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
"""


def test_filter_covers_only_the_loaded_window(tmp_path: Path) -> None:
    newest = [_mem(1, ["recent"], f"recent {i}") for i in range(1000)]
    oldest = _mem(2, ["old-only"], "oldest")
    page = _page([[*newest, oldest]]).replace(_PROBE, _WINDOW_PROBE)
    out = _render(tmp_path, page, 1000)
    assert "recent" in out["menu"]
    assert "old-only" not in out["menu"]
    assert "store has 1001" in out["meta"]
