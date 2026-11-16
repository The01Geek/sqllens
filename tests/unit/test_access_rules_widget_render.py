# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Render the access-rules widget in headless Chromium against a stub host (#404).

Skipped when no Chromium-family binary is found; set ``SQLLENS_TEST_CHROMIUM``
to a binary path to run it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sqllens.access_rules import access_rules_json
from sqllens.config import (
    AccessRulesConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    RlsRule,
    RuleSetsConfig,
)
from sqllens.ui import load_widget_html

from ._rule_set_fixtures import MYSQL_URL, example_document, write_rule_set_file
from .test_access_rules import BIG, _all_kinds_document


def _chromium() -> str | None:
    env = os.environ.get("SQLLENS_TEST_CHROMIUM")
    if env:
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell"):
        found = shutil.which(name)
        if found:
            return found
    cache = Path.home() / ".cache/ms-playwright"
    pattern = "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
    shells = sorted(cache.glob(pattern))
    return str(shells[-1]) if shells else None


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="headless Chromium not available")

# Runs after the widget's first render and writes what it observes into the DOM.
_PROBE = """
setTimeout(() => {
  const out = {};
  const q = (s) => document.querySelector(s);
  out.header = q("#header").innerText;
  out.entries = [...document.querySelectorAll("#entries li")].map((li) => li.innerText);
  out.detail = q("#detail").innerText;
  out.error = q("#errbar").classList.contains("show") ? q("#errbar").innerText : null;
  out.columns = getComputedStyle(q(".layout")).gridTemplateColumns.split(" ").length;
  out.inputs = document.querySelectorAll("input").length;
  out.buttons = document.querySelectorAll("button, select, textarea, form").length;
  if (window.__steps) for (const step of window.__steps) step(out);
  const pre = document.createElement("pre");
  pre.id = "probe";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
}, 200);
"""


def _page(result: dict[str, Any] | None, steps: str = "") -> str:
    html = load_widget_html("access_rules.html")
    match = re.search(r"var App = \w+;", html)
    assert match
    if result is None:
        call = "return new Promise(() => {});"
    else:
        call = (
            "return Promise.resolve({isError: " + json.dumps(result["isError"])
            + ", content: [{type: 'text', text: " + json.dumps(result["text"]) + "}]});"
        )
    stub = (
        "var App = class { connect() { return Promise.resolve(); }"
        " getHostContext() { return null; } callServerTool(req) {"
        " window.__calls = (window.__calls || []).concat([req.name]); " + call + " } };"
    )
    page = html[: match.start()] + stub + html[match.end():]
    return page.replace("</script>\n</body>", steps + _PROBE + "</script>\n</body>")


def _render(tmp_path: Path, page: str, width: int = 1000) -> dict[str, Any]:
    path = tmp_path / "widget.html"
    path.write_text(page, encoding="utf-8")
    proc = subprocess.run(
        [CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu", f"--window-size={width},900",
         "--virtual-time-budget=3000", "--dump-dom", path.as_uri()],
        capture_output=True, text=True, timeout=60, check=False,
    )
    match = re.search(r'<pre id="probe">(.*?)</pre>', proc.stdout, re.S)
    assert match, proc.stderr[-2000:]
    import html as html_module

    return json.loads(html_module.unescape(match.group(1)))


def _full_json(tmp_path: Path) -> str:
    document = _all_kinds_document()
    document["rule_sets"].append(example_document()["rule_sets"][1])
    cfg = Config(
        database=DatabaseConfig(url=MYSQL_URL),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        rls=[
            RlsRule(table="orders", column="a", value=float("nan")),
            RlsRule(table="orders", column="b", value=BIG),
            RlsRule(table="orders", column="c", value=1.0),
            RlsRule(table="orders", column="d", value=float("inf")),
            RlsRule(table="orders", column="e", value="inf"),
            RlsRule(table="orders", column="f", value=True),
        ],
        rule_sets=RuleSetsConfig(file=write_rule_set_file(tmp_path, document)),
        access_rules=AccessRulesConfig(allow_admin_tools=True),
    )
    return access_rules_json(cfg)


def test_loading_state(tmp_path: Path) -> None:
    out = _render(tmp_path, _page(None))
    assert out["detail"] == "Loading…"


def test_flat_rls_values_keep_text_and_type(tmp_path: Path) -> None:
    out = _render(tmp_path, _page({"isError": False, "text": _full_json(tmp_path)}))
    detail = out["detail"]
    for shown in ("nan\tfloat", f"{BIG}\tint", "1.0\tfloat", "inf\tfloat", '"inf"\tstr',
                  "true\tbool"):
        text, kind = shown.split("\t")
        assert re.search(re.escape(text) + r"\s*" + kind, detail), (shown, detail)
    assert "Loaded at" in out["header"]
    assert "SHA-256" in out["header"] and "not yet" in out["header"]
    assert out["entries"][0].startswith("Flat RLS")
    assert any("Rule set 3" in e and "unconfirmed" in e for e in out["entries"])
    assert not any("Rule set 2" in e and "unconfirmed" in e for e in out["entries"])
    assert out["error"] is None
    assert out["inputs"] == 1 and out["buttons"] == 0
    assert out["columns"] == 2


_SELECT_SET_2 = """
window.__steps = [(out) => {
  const li = [...document.querySelectorAll('#entries li')]
    .find((x) => x.innerText.startsWith('Rule set 2'));
  li.click();
  out.collapsed = [...document.querySelectorAll('details.rule')].every((d) => !d.open);
  document.querySelectorAll('details.rule').forEach((d) => { d.open = true; });
  out.set_detail = document.querySelector('#detail').innerText;
  const search = document.getElementById('search');
  search.value = 'tags';
  search.dispatchEvent(new Event('input'));
  out.search_entries = [...document.querySelectorAll('#entries li')].map((x) => x.innerText);
  out.search_tables = [...document.querySelectorAll('#detail .card h3')].map((x) => x.innerText);
  search.value = 'zzz';
  search.dispatchEvent(new Event('input'));
  out.hidden_detail = document.querySelector('#detail').innerText;
}];
"""


def test_rule_set_detail_trees_and_search(tmp_path: Path) -> None:
    out = _render(tmp_path, _page({"isError": False, "text": _full_json(tmp_path)}, _SELECT_SET_2))
    assert out["collapsed"] is True
    detail = out["set_detail"]
    for kind in ("compare", "is_null", "is_not_null", "any_of", "all_of", "owner_via",
                 "inherit", "referenced_by", "parent_rule:", "link"):
        assert kind in detail, kind
    assert re.search(re.escape(str(BIG)) + r"\s*int", detail)
    assert out["search_entries"] == [out["search_entries"][0], *[
        e for e in out["search_entries"][1:] if "Rule set 2" in e]]
    assert set(out["search_tables"]) == {"source", "order_tags", "tags"}
    assert out["hidden_detail"] == "The selected rule set does not match the search."


def test_empty_state(tmp_path: Path) -> None:
    cfg = Config(database=DatabaseConfig(url=MYSQL_URL), llm=LLMConfig(api_key=SecretStr("k")))
    out = _render(tmp_path, _page({"isError": False, "text": access_rules_json(cfg)}))
    assert out["detail"] == "No access rules are loaded."


def test_error_state(tmp_path: Path) -> None:
    body = json.dumps({"error": "rule_sets.file is set but the rule-set file was never loaded"})
    out = _render(tmp_path, _page({"isError": True, "text": body}))
    assert out["error"] == (
        "get_access_rules: rule_sets.file is set but the rule-set file was never loaded"
    )
    assert out["detail"] == "The access rules could not be loaded."


def test_narrow_width_stacks_panes(tmp_path: Path) -> None:
    out = _render(tmp_path, _page({"isError": False, "text": _full_json(tmp_path)}), width=420)
    assert out["columns"] == 1
