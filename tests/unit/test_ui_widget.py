# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.ui`` widget-asset loading.

Pins three deliberate contracts the widget loader makes (the asset is shipped
via the wheel include globs in ``pyproject.toml``; a drop or truncation must
fail loudly, not silently render a blank iframe):

- a missing/unreadable asset raises an *actionable* ``RuntimeError`` rather
  than FastMCP's generic resource error,
- an empty/truncated asset raises the same actionable error instead of being
  cached and rendered as a blank iframe, and
- ``@cache`` must not poison-cache a read failure — a transient/packaging
  fault must re-attempt (and succeed) on the next fetch.
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
    def fake_read() -> str:
        try:
            raise FileNotFoundError("query_results.html")
        except FileNotFoundError as e:
            raise RuntimeError(
                "query_database result widget asset is unavailable; "
                "reinstall sqllens or check the wheel packaging"
            ) from e

    monkeypatch.setattr(ui, "_read_widget_html", fake_read)
    with pytest.raises(RuntimeError, match="widget asset is unavailable"):
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
    with pytest.raises(RuntimeError, match="asset is empty"):
        ui.load_widget_html()


def test_read_failure_is_not_poison_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def flaky() -> str:
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


def test_successful_read_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def once() -> str:
        calls["n"] += 1
        return "<html>cached</html>"

    monkeypatch.setattr(ui, "_read_widget_html", once)
    assert ui.load_widget_html() == "<html>cached</html>"
    assert ui.load_widget_html() == "<html>cached</html>"
    assert calls["n"] == 1  # success memoized — read once
