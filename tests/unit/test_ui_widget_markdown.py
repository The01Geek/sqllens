# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for the query-results widget's Markdown text rendering.

Text blocks in ``_meta["sqllens/blocks"]`` carry agent-authored Markdown. The
widget used to paint them via ``textContent`` so hosts showed raw ``#`` /
``**`` / ``-`` syntax. It now renders them with the vendored markdown-it,
configured by ``createMarkdownRenderer`` (between the ``md-renderer:start`` /
``md-renderer:end`` markers in ``query_results.html``). That configuration is
the widget's XSS boundary — its output is assigned to ``innerHTML`` — so these
tests pin it: raw HTML escaped, unsafe link schemes and images inert.

The repo has no browser JS harness, so the tests run the extracted factory
under Node with the vendored ``markdown-it.min.js``. Skipped when ``node`` is
not on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib.resources import files

import pytest

import sqllens.ui as ui

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute the widget JS")


def _renderer_source() -> str:
    html = ui.load_widget_html()
    m = re.search(r"/\* md-renderer:start \*/(.*?)/\* md-renderer:end \*/", html, re.S)
    assert m, "md-renderer markers missing from query_results.html"
    return m.group(1)


def render(markdown: str) -> str:
    """Render ``markdown`` exactly as the widget does; newlines dropped for asserts."""
    bundle = str(files("sqllens.ui").joinpath("vendor", "markdown-it.min.js"))
    script = (
        "import { createRequire } from 'node:module';"
        f"\nconst markdownit = createRequire(import.meta.url)({json.dumps(bundle)});"
        + _renderer_source()
        + "\nconst md = createMarkdownRenderer(markdownit);"
        + f"\nprocess.stdout.write(md.render({json.dumps(markdown)}));"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.replace("\n", "")


def test_agent_report_renders_headings_bold_and_lists() -> None:
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


def test_raw_html_is_escaped_not_injected() -> None:
    # html: false — model output must never become live markup (innerHTML sink).
    out = render('<img src=x onerror="alert(1)">\n\n**<b>x</b>** <script>alert(1)</script>')
    assert "<img" not in out and "<b>" not in out and "<script" not in out
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in out
    assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in out


def test_unsafe_link_schemes_stay_literal_and_safe_links_open_externally() -> None:
    out = render(
        "[docs](https://example.com/a) [js](javascript:alert(1)) "
        "[vb](vbscript:x) [data](data:text/html;base64,PHNjcmlwdD4=)"
    )
    anchor = '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer">docs</a>'
    assert anchor in out
    assert out.count("<a ") == 1
    assert 'href="javascript' not in out and 'href="vbscript' not in out
    assert 'href="data' not in out


def test_images_are_not_rendered() -> None:
    # The widget makes no remote requests; image syntax must not fetch a URL.
    out = render("![chart](https://example.com/x.png)")
    assert "<img" not in out
    # Image syntax degrades to "!" + an ordinary (user-clicked) link.
    assert '!<a href="https://example.com/x.png"' in out


def test_snake_case_identifiers_are_not_italicised() -> None:
    out = render("Grouped by total_gross_revenue and _private_ note")
    assert "total_gross_revenue" in out
    assert out.count("<em>") == 1 and "<em>private</em>" in out


def test_fenced_code_is_literal() -> None:
    out = render("**Executed SQL:**\n```sql\nSELECT * FROM t WHERE a < 2 -- **x**\n```")
    assert out == (
        "<p><strong>Executed SQL:</strong></p>"
        '<pre><code class="language-sql">SELECT * FROM t WHERE a &lt; 2 -- **x**</code></pre>'
    )


def test_soft_line_breaks_are_kept() -> None:
    # breaks: true — the agent uses single newlines for layout.
    assert render("use `a_b * c`\nnext line") == "<p>use <code>a_b * c</code><br>next line</p>"


def test_pipe_tables_are_wrapped_in_scroll_container() -> None:
    out = render("| Product | Units |\n|---|---:|\n| Coolant | 90 |")
    assert out.startswith('<div class="wrap"><table><thead><tr><th>Product</th>')
    assert out.endswith("</table></div>")
    assert "<td>Coolant</td>" in out
