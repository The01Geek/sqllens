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
- both widgets inline their vendored JS bundles so MCP App hosts (which
  ``document.write`` the HTML into an about:blank-base iframe) can run the
  scripts without a 404 on relative paths.
"""

from __future__ import annotations

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


def test_query_widget_inlines_app_sdk_bundle() -> None:
    html = ui.load_widget_html("query_results.html")
    # Relative module import must be gone after inlining.
    assert 'import { App } from "./vendor/app-with-deps.js"' not in html
    # The local binding the widget script consumes must be present.
    assert "var App = " in html
    # Sentinel that only the inlined ext-apps SDK carries.
    assert "ui/notifications/tool-result" in html


def test_chart_widget_inlines_app_sdk_and_echarts() -> None:
    html = ui.load_widget_html("chart_results.html")
    # No relative references should survive — both would 404 in an MCP App
    # sandbox iframe.
    assert 'import { App } from "./vendor/app-with-deps.js"' not in html
    assert '<script src="./vendor/echarts.min.js">' not in html
    # Sentinels for both inlined bundles.
    assert "var App = " in html
    assert "ui/notifications/tool-result" in html
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

    html = '<html><script type="module">' + ui._APP_SDK_IMPORT + "</script></html>"
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

    html = '<html><script type="module">' + ui._APP_SDK_IMPORT + "</script></html>"
    monkeypatch.setattr(
        ui, "files", lambda _pkg: _Stub(html, "var x=1;\nexport { x as NotApp };")
    )
    with pytest.raises(RuntimeError, match="does not export `App`"):
        ui.load_widget_html("query_results.html")
