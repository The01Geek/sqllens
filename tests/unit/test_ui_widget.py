# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.ui`` widget-asset loading.

Pins the contracts the widget loader makes (assets are shipped via the wheel
include globs in ``pyproject.toml``; a drop, truncation, or inliner break must
fail loudly, not silently render a blank iframe):

- a missing/unreadable asset (HTML or vendored bundle) raises an *actionable*
  ``RuntimeError`` rather than FastMCP's generic resource error,
- an empty/truncated asset raises the same actionable error instead of being
  cached and rendered as a blank iframe,
- ``@cache`` must not poison-cache a read failure — a transient/packaging
  fault must re-attempt (and succeed) on the next fetch,
- the unified widget inlines BOTH vendored JS bundles (ext-apps SDK + echarts)
  so MCP App hosts (which ``document.write`` the HTML into an about:blank-base
  iframe) can run the scripts without a 404 on relative paths.
"""

from __future__ import annotations

import re

import pytest

import sqllens.ui as ui


@pytest.fixture(autouse=True)
def _clear_widget_cache():
    # load_widget_html is @cache'd at module scope; isolate each test.
    ui.load_widget_html.cache_clear()
    yield
    ui.load_widget_html.cache_clear()


def test_missing_asset_raises_actionable_runtimeerror(monkeypatch) -> None:
    # Patch the resource read indirectly: simulate the wheel missing the asset.
    def fake_read(filename: str) -> str:
        try:
            raise FileNotFoundError(filename)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"result widget asset {filename!r} is unavailable; "
                "reinstall sqllens or check the wheel packaging"
            ) from e

    monkeypatch.setattr(ui, "_read_widget_html", fake_read)
    with pytest.raises(RuntimeError, match=r"widget asset .* is unavailable"):
        ui.load_widget_html()


def test_real_read_failure_surfaces_runtimeerror(monkeypatch) -> None:
    # Exercise the real _read_widget_html error mapping by forcing the
    # importlib.resources read to raise the documented FileNotFoundError.
    # `files` is bound into the ui module namespace at import time, so patch
    # the module attribute, not importlib.resources.

    class _Missing:
        def joinpath(self, *_a, **_k):
            return self

        def read_text(self, *_a, **_k):
            raise FileNotFoundError("query_results.html")

    monkeypatch.setattr(ui, "files", lambda _pkg: _Missing())
    with pytest.raises(RuntimeError, match="reinstall sqllens"):
        ui.load_widget_html()


def test_empty_asset_raises_actionable_runtimeerror(monkeypatch) -> None:
    # A truncated wheel asset reads as an empty/whitespace string; it must not
    # be memoized and rendered as a blank iframe.
    class _Empty:
        def joinpath(self, *_a, **_k):
            return self

        def read_text(self, *_a, **_k):
            return "   \n\t  "

    monkeypatch.setattr(ui, "files", lambda _pkg: _Empty())
    with pytest.raises(RuntimeError, match=r"asset .* is empty"):
        ui.load_widget_html()


def test_read_failure_is_not_poison_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def flaky(filename: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient packaging fault")
        return "<html>ok</html>"

    monkeypatch.setattr(ui, "_read_widget_html", flaky)

    with pytest.raises(RuntimeError, match="transient packaging fault"):
        ui.load_widget_html()
    # Second fetch must re-attempt (failure not memoized by @cache) and succeed.
    assert ui.load_widget_html() == "<html>ok</html>"
    assert calls["n"] == 2


def test_widget_asset_wires_executed_sql_section() -> None:
    # No JS test harness exists in the repo, so AC #4 (the widget renders a
    # collapsible "Executed SQL" section from _meta["sqllens/query"] and
    # degrades when absent) cannot be exercised behaviorally here. This
    # structural guard at least fails loudly if a future re-lift or edit drops
    # the SQL-section wiring: the meta key constant and the two-host split
    # (sqlHost painted once by ingest(), gridHost re-cleared by render()).
    html = ui.load_widget_html()
    assert 'QUERY_META_KEY = "sqllens/query"' in html
    assert "sqlHost" in html
    assert "gridHost" in html


def test_widget_dispatch_pins_chart_wins_precedence() -> None:
    # The repo has no JS test harness, so the widget's chart > table > text
    # render-mode dispatch (the one genuinely-new behavior of the consolidation)
    # cannot be exercised behaviorally here. This structural guard fails loudly
    # if a refactor drops the load-bearing predicate: (a) the chart channel is
    # consulted, (b) chart mode is entered only when chart data is non-empty —
    # so an empty/malformed chart payload falls through to the table rather than
    # hiding a present grid behind a blank chart, and (c) both render-mode
    # entrypoints exist.
    html = ui.load_widget_html()
    assert 'const CHART_META_KEY = "sqllens/chart";' in html
    assert 'const TABLE_META_KEY = "sqllens/table";' in html
    # chart-wins guard: chart mode requires a non-empty data array.
    assert "chartPayload.data.length > 0" in html
    assert "ingestChart(chartPayload)" in html
    assert "ingestTable(meta)" in html


def test_widget_auto_fits_iframe_via_sdk_autoresize() -> None:
    # Issue #174: in a sandboxed cross-origin iframe the host can't read the
    # document, so the page must report its own size. The App SDK does this when
    # constructed with `autoResize` — it emits the protocol's
    # `ui/notifications/size-changed` with the true content height. Two things
    # must hold for the fit to work, and neither can be exercised behaviorally
    # (no JS harness), so guard them structurally:
    #   1. autoResize is requested explicitly (not left to the SDK default).
    #   2. chart mode is content-sized — no `100vh`, which the SDK's
    #      `max-content` measurement cannot collapse, so it would report the
    #      full iframe height and never shrink to fit.
    html = ui.load_widget_html()
    # autoResize must be the App's THIRD positional arg (options), not just
    # present somewhere — the SDK reads it from `this.options`, so a regression
    # that moved it into the capabilities arg would silently fall back to the
    # default. Pin it directly after the empty capabilities object.
    assert re.search(r"\{\},\s*\{\s*autoResize:\s*true\s*\}", html) is not None
    # The size-changed-capable SDK must be inlined (vendor-drift guard); the
    # wiring itself is exercised by the autoResize assertion above, not here.
    assert "ui/notifications/size-changed" in html
    # Chart mode must stay content-sized: a definite #chart height is the
    # mechanism the SDK's `max-content` measurement relies on, and the prior
    # viewport-height trap (`100vh`) must not return.
    assert "#chart { height: 360px" in html
    assert "100vh" not in html


def test_successful_read_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def once(filename: str) -> str:
        calls["n"] += 1
        return "<html>cached</html>"

    monkeypatch.setattr(ui, "_read_widget_html", once)
    assert ui.load_widget_html() == "<html>cached</html>"
    assert ui.load_widget_html() == "<html>cached</html>"
    assert calls["n"] == 1  # success memoized — read once


# --- Inlining contract -------------------------------------------------------
#
# MCP App hosts ``document.write`` the widget HTML into an iframe whose base
# URL is about:blank — sibling ``./vendor/...`` files on the MCP server are
# unreachable from that scope. The loader inlines the bundles to keep the
# widget self-contained over a single ``ui://`` fetch. These tests guard the
# inliner so a regression (re-introducing the relative import/script-src,
# breaking the bundle splice, or upstream renaming the App export) fails the
# suite instead of only failing at MCP-host render time.


def test_widget_inlines_app_sdk_and_echarts() -> None:
    # The unified widget renders either a chart or a grid, so it inlines BOTH
    # vendored bundles. No relative references should survive — either would
    # 404 in an MCP App sandbox iframe.
    html = ui.load_widget_html("query_results.html")
    assert 'import { App } from "./vendor/app-with-deps.js"' not in html
    assert '<script src="./vendor/echarts.min.js">' not in html
    # Sentinels for both inlined bundles.
    assert "var App = " in html
    assert "ui/notifications/tool-result" in html  # ext-apps SDK
    assert "Apache Software Foundation" in html  # echarts license header
    assert "echarts.init" in html  # widget call into the inlined global


def test_app_sdk_bundle_missing_export_raises(monkeypatch) -> None:
    class _Stub:
        def __init__(self, html: str, bundle: str) -> None:
            self.html, self.bundle = html, bundle

        def joinpath(self, *parts):
            self._target = "bundle" if parts and parts[0] == "vendor" else "html"
            return self

        def read_text(self, *_a, **_k):
            return self.html if self._target == "html" else self.bundle

    # The unified widget's recipe inlines echarts first, then the app SDK, so
    # the minimal HTML must carry both relative refs for the loader to reach
    # the app-SDK splice that these tests exercise.
    html = (
        "<html>"
        + ui._ECHARTS_SCRIPT_TAG
        + '<script type="module">'
        + ui._APP_SDK_IMPORT
        + "</script></html>"
    )
    # A bundle without the trailing `export { ... };` clause must fail loudly.
    monkeypatch.setattr(ui, "files", lambda _pkg: _Stub(html, "var nope = 1;\n"))
    with pytest.raises(RuntimeError, match="trailing"):
        ui.load_widget_html("query_results.html")


def test_app_sdk_bundle_without_app_export_raises(monkeypatch) -> None:
    class _Stub:
        def __init__(self, html: str, bundle: str) -> None:
            self.html, self.bundle = html, bundle

        def joinpath(self, *parts):
            self._target = "bundle" if parts and parts[0] == "vendor" else "html"
            return self

        def read_text(self, *_a, **_k):
            return self.html if self._target == "html" else self.bundle

    # The unified widget's recipe inlines echarts first, then the app SDK, so
    # the minimal HTML must carry both relative refs for the loader to reach
    # the app-SDK splice that these tests exercise.
    html = (
        "<html>"
        + ui._ECHARTS_SCRIPT_TAG
        + '<script type="module">'
        + ui._APP_SDK_IMPORT
        + "</script></html>"
    )
    monkeypatch.setattr(
        ui, "files", lambda _pkg: _Stub(html, "var x=1;\nexport { x as NotApp };")
    )
    with pytest.raises(RuntimeError, match="does not export `App`"):
        ui.load_widget_html("query_results.html")


# --- memory_admin.html contracts --------------------------------------------
#
# The memory-administration widget (issue #188) is a self-driving panel: it
# pulls its own data on mount via App.callServerTool and drives every admin
# tool directly. Two structural contracts must hold (no JS harness — assert
# the markers that prove them):
#   1. Topology-agnostic — no direct network reachability assumed. The widget
#      must not open its own connection (no fetch/XHR/WebSocket, no <script
#      src> to a server origin), so it renders identically whether the host
#      reaches the server directly or via a proxy.
#   2. isError contract — partial add_memories and lossy export_memories must
#      paint a loud red error, never green success.


def test_memory_admin_widget_inlines_both_bundles() -> None:
    # Same inlining contract as query_results.html: the host document.writes
    # the HTML into an about:blank-base iframe; any surviving relative
    # ./vendor/... reference would 404.
    html = ui.load_widget_html("memory_admin.html")
    assert 'import { App } from "./vendor/app-with-deps.js"' not in html
    assert '<script src="./vendor/echarts.min.js">' not in html
    assert "var App = " in html
    assert "echarts.init" in html


def test_memory_admin_widget_makes_no_direct_network_calls() -> None:
    # Topology-agnostic AC: the widget must broker every server call through
    # the host (App.callServerTool). It must not open its own connection.
    # Audit the *raw* widget source (not the inlined output) — the vendored
    # ECharts bundle ships with license-header URL comments that would
    # false-positive a substring check against the full inlined HTML.
    from importlib.resources import files
    raw = files("sqllens.ui").joinpath("memory_admin.html").read_text(encoding="utf-8")
    # callServerTool is the only I/O channel.
    assert "app.callServerTool" in raw
    # No browser network APIs — the widget cannot have a network surface that
    # doesn't surface via one of these names. A behavioral assertion proxy.
    forbidden = ["fetch(", "XMLHttpRequest", "new WebSocket", "EventSource", "navigator.sendBeacon"]
    leaks = [name for name in forbidden if name in raw]
    assert leaks == [], f"widget has direct network surface: {leaks}"
    # No remote <script src> in the raw widget either. The two ./vendor refs
    # are spliced out by the inliner; both should be relative paths only.
    assert 'src="http' not in raw and "src='http" not in raw
    # No remote ESM import: every `import ... from "..."` in the widget must
    # resolve to a relative path (the inliner replaces ./vendor/...).
    remote_imports = re.findall(r'import[^\n;]+from\s+["\']https?://', raw)
    assert remote_imports == [], f"widget has remote ESM imports: {remote_imports}"


def test_memory_admin_widget_self_drives_on_mount() -> None:
    # AC: "On mount the widget renders the store via callServerTool
    # (list_memories + get_memory_stats) with no model involvement." Pin the
    # mount-time tool wiring structurally.
    html = ui.load_widget_html("memory_admin.html")
    assert "loadBrowse()" in html
    assert "loadStats()" in html
    # The two boot-time tools are referenced by their server-side names.
    assert '"list_memories"' in html
    assert '"get_memory_stats"' in html
    # The destructive trio appears too (each section drives its tool).
    driven_tools = (
        '"add_memories"', '"export_memories"',
        '"delete_memory"', '"clear_memories"', '"get_memory"',
    )
    for name in driven_tools:
        assert name in html, f"widget no longer drives {name}"


def test_memory_admin_widget_paints_red_on_iserror() -> None:
    # The isError contract requires the widget to honor result.isError from
    # the central callTool wrapper. Without a JS harness, pin the structural
    # invariants that make red-state unavoidable:
    #   - the wrapper reads result.isError (not e.g. only the message text),
    #   - partial add_memories (errors[] non-empty) routes to showError,
    #   - lossy export_memories (lossy: true) routes to showError.
    html = ui.load_widget_html("memory_admin.html")
    assert "result.isError" in html or "result && result.isError" in html
    # The wrapper must propagate isError to callers; callers must branch on it.
    assert "res.isError" in html
    # Lossy export and partial-import detection by data shape.
    assert "data.lossy" in html
    assert "Array.isArray(data.errors)" in html
    # Both error renderers exist and the destructive flows confirm-gate.
    assert "showError(" in html
    assert "confirm(" in html  # window.confirm gate for destructive actions


def test_memory_admin_widget_reads_real_wire_field_names() -> None:
    # The server-side wire shape produced by `_record_to_wire` in
    # `src/sqllens/memory/admin.py` is `memory_id` / `memory_type` /
    # `timestamp` / `last_hit_date`. Pre-fix the widget read `id` / `type` /
    # `created_at` / `last_hit_at`, which exist only in the dev-shim mock —
    # against a real server, Browse table rows rendered with empty
    # id/type/created columns, row clicks called `get_memory` with
    # `memory_id: undefined`, and stats chart bars had blank y-axis labels.
    # Pin the canonical field names so this drift cannot return; absence
    # of the legacy names is asserted via a separate guard below.
    html = ui.load_widget_html("memory_admin.html")
    for canonical in ("m.memory_id", "m.memory_type", "m.timestamp", "m.last_hit_date"):
        assert canonical in html, f"widget no longer reads wire field {canonical}"
    # The legacy mock-only names must NOT appear as widget reads. Use a
    # word-boundary regex so `m.id` doesn't match `m.identifier` or
    # similar (none exist today, but the guard outlives them).
    import re as _re
    raw = ui._read_text("memory_admin.html") if hasattr(ui, "_read_text") else html
    for legacy in ("id", "type", "created_at", "last_hit_at"):
        # Match `m.id` / `m.type` / etc as a complete dotted-access read.
        pattern = _re.compile(r"\bm\." + legacy + r"\b")
        assert not pattern.search(raw), (
            f"widget still reads legacy mock-only field m.{legacy}; "
            "should be the wire shape from _record_to_wire"
        )


def test_memory_admin_widget_clear_confirm_is_uppercase_token() -> None:
    # Type-to-confirm: the "Clear" action must require the user to type
    # "CLEAR" exactly. A regression that swaps it for a click-only confirm
    # would weaken the danger gate.
    html = ui.load_widget_html("memory_admin.html")
    assert 'confirmText !== "CLEAR"' in html


