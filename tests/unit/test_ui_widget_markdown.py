# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for the query-results widget's Markdown text renderer.

Text blocks in ``_meta["sqllens/blocks"]`` carry agent-authored Markdown. The
widget used to paint them via ``textContent`` so hosts showed raw ``#`` /
``**`` / ``-`` syntax. The renderer between the ``md-renderer:start`` /
``md-renderer:end`` markers in ``query_results.html`` now builds real DOM.

The repo has no browser JS harness, so these tests extract that renderer
source, run it under Node against a tiny ``document`` shim that serialises
the built tree to HTML, and assert on the output. Skipped when ``node`` is
not on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

import sqllens.ui as ui

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute the widget JS")

# Minimal DOM: just enough surface for the renderer (createElement,
# createTextNode, appendChild, textContent, className, setAttribute) plus an
# escaping serialiser so assertions see exactly what a browser would build.
_DOM_SHIM = r"""
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
class Text { constructor(t) { this.data = String(t); } html() { return esc(this.data); } }
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set className(v) { this.attrs["class"] = v; }
  set textContent(v) { this.children = [new Text(v)]; }
  html() {
    const a = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${esc(v)}"`).join("");
    if (this.tag === "br" || this.tag === "hr") return `<${this.tag}${a}>`;
    return `<${this.tag}${a}>${this.children.map((c) => c.html()).join("")}</${this.tag}>`;
  }
}
globalThis.document = {
  createElement: (t) => new El(t),
  createTextNode: (t) => new Text(t),
};
"""


def _renderer_source() -> str:
    html = ui.load_widget_html()
    m = re.search(r"/\* md-renderer:start \*/(.*?)/\* md-renderer:end \*/", html, re.S)
    assert m, "md-renderer markers missing from query_results.html"
    return m.group(1)


def render(markdown: str) -> str:
    """Render ``markdown`` through the widget's renderer; return the inner HTML."""
    script = (
        _DOM_SHIM
        + _renderer_source()
        + "\nconst root = document.createElement('div');"
        + f"\nrenderMarkdown({json.dumps(markdown)}, root);"
        + "\nprocess.stdout.write(root.children.map((c) => c.html()).join(''));"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module", "-"], input=script,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_headings_bold_and_lists_render_as_elements() -> None:
    # The exact shape of the agent text that surfaced raw syntax in a host.
    out = render(
        "# Sales Report\n**Huntington Honda - June 2026**\n\n## Summary Totals\n"
        "- **Total Units Sold:** 592\n- **Total Gross Revenue:** $84,281.55\n\n"
        "1. **Differential Fluid Exchange** - 125 units\n2. **Coolant Service** - 90 units"
    )
    assert out == (
        "<h1>Sales Report</h1>"
        "<p><strong>Huntington Honda - June 2026</strong></p>"
        "<h2>Summary Totals</h2>"
        "<ul><li><strong>Total Units Sold:</strong> 592</li>"
        "<li><strong>Total Gross Revenue:</strong> $84,281.55</li></ul>"
        "<ol><li><strong>Differential Fluid Exchange</strong> - 125 units</li>"
        "<li><strong>Coolant Service</strong> - 90 units</li></ol>"
    )


def test_markup_in_text_is_escaped_not_injected() -> None:
    # Model output must never become live markup inside the widget.
    out = render('<img src=x onerror="alert(1)"> **<b>x</b>**')
    assert "<img" not in out and "<b>" not in out
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in out
    assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in out


def test_only_safe_link_schemes_become_anchors() -> None:
    out = render("[docs](https://example.com/a) and [bad](javascript:alert(1))")
    anchor = '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer">docs</a>'
    assert anchor in out
    assert "javascript" in out and 'href="javascript' not in out


def test_snake_case_identifiers_are_not_italicised() -> None:
    out = render("Grouped by total_gross_revenue and _private_ note")
    assert "total_gross_revenue" in out
    assert out.count("<em>") == 1 and "<em>private</em>" in out


def test_fenced_code_is_literal() -> None:
    out = render("**Executed SQL:**\n```sql\nSELECT * FROM t WHERE a < 2 -- **x**\n```")
    assert out == (
        "<p><strong>Executed SQL:</strong></p>"
        '<pre><code class="lang-sql">SELECT * FROM t WHERE a &lt; 2 -- **x**</code></pre>'
    )


def test_inline_code_and_soft_line_breaks() -> None:
    assert render("use `a_b * c`\nnext line") == "<p>use <code>a_b * c</code><br>next line</p>"


def test_nested_list_and_mixed_list_types() -> None:
    out = render("- parent\n  - child\n- sibling\n1. first")
    assert out == (
        "<ul><li>parent<ul><li>child</li></ul></li><li>sibling</li></ul>"
        "<ol><li>first</li></ol>"
    )


def test_pipe_table_renders_with_ragged_rows_padded() -> None:
    out = render("| Product | Units |\n|---|---:|\n| Coolant | 90 |\n| Only one |")
    assert out == (
        '<div class="wrap"><table><thead><tr><th>Product</th><th>Units</th></tr></thead>'
        "<tbody><tr><td>Coolant</td><td>90</td></tr><tr><td>Only one</td><td></td></tr>"
        "</tbody></table></div>"
    )


def test_blockquote_and_horizontal_rule() -> None:
    assert render("> **note**\n\n---") == (
        "<blockquote><p><strong>note</strong></p></blockquote><hr>"
    )


def test_unterminated_markers_stay_literal() -> None:
    assert render("price * 2 and ** alone and `tick") == (
        "<p>price * 2 and ** alone and `tick</p>"
    )
