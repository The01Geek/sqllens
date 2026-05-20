# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Packaged MCP App widget assets.

The HTML widgets and their vendored JS bundles ship inside the wheel (see the
``[tool.hatch.build.targets.wheel].include`` globs in ``pyproject.toml``).
``server.py`` serves :func:`load_widget_html` results as the ``ui://``
resources an apps-aware host renders in sandboxed iframes — one for
``query_database`` (``query_results.html``) and one for ``visualize_data``
(``chart_results.html``).

The vendored JS bundles are *inlined* into the HTML at load time. MCP App
hosts only fetch the single ``ui://`` resource — they then ``document.write``
the HTML into an ``about:blank``-base iframe whose origin cannot resolve
sibling files on the MCP server. A relative ``import "./vendor/…"`` or
``<script src="./vendor/…">`` will 404 inside that iframe, the script never
runs, and the widget hangs on its initial ``Waiting for results…``
placeholder. Inlining keeps the on-disk multi-file layout (so the vendored
bundles' SHA bookkeeping in ``vendor/README`` still works for upgrades) and
ships a single self-contained HTML payload to the host.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import cache
from importlib.resources import files

logger = logging.getLogger("sqllens.ui")

# query_results.html only uses the ext-apps SDK (ESM module import).
# chart_results.html uses BOTH the ext-apps SDK and echarts (classic UMD).
_APP_SDK_IMPORT = 'import { App } from "./vendor/app-with-deps.js";'
_ECHARTS_SCRIPT_TAG = '<script src="./vendor/echarts.min.js"></script>'

# The ext-apps bundle ends with one ESM export block ``export { ... eI as App };``
# from which we extract the local identifier for ``App``. Used by both widgets.
_BUNDLE_EXPORT_RE = re.compile(r"export\s*\{([^}]*)\}\s*;?\s*$")
_APP_ALIAS_RE = re.compile(r"(\w+)\s+as\s+App\b")


def _inline_app_sdk(html: str, bundle: str) -> str:
    """Splice the ext-apps SDK bundle into a widget's ``<script type="module">``.

    Strips the bundle's trailing ESM ``export { ... };`` clause (module
    exports are inert inside an inlined ``<script type="module">``) and
    replaces it with a local ``var App = <identifier>;`` so the rest of
    the widget script can use ``App`` exactly as it did with the import.
    """
    m = _BUNDLE_EXPORT_RE.search(bundle)
    if m is None:
        raise RuntimeError(
            "vendored ext-apps bundle is missing the expected trailing "
            "`export { ... };` clause; reinstall sqllens or check "
            "src/sqllens/ui/vendor/README for upgrade instructions"
        )
    alias = _APP_ALIAS_RE.search(m.group(1))
    if alias is None:
        raise RuntimeError(
            "vendored ext-apps bundle does not export `App`; "
            "the upgrade in src/sqllens/ui/vendor/ may have shipped a "
            "different SDK"
        )
    bundle_inlined = bundle[: m.start()] + f"var App = {alias.group(1)};"
    if _APP_SDK_IMPORT not in html:
        raise RuntimeError(
            "widget HTML no longer imports App from "
            "./vendor/app-with-deps.js; the inliner has nothing to replace"
        )
    return html.replace(_APP_SDK_IMPORT, bundle_inlined)


def _inline_echarts(html: str, bundle: str) -> str:
    """Replace the chart widget's ``<script src=echarts.min.js>`` with inline.

    ECharts ships as a UMD bundle that attaches ``window.echarts`` on load.
    The classic ``<script>`` tag is parsed/executed before the deferred
    ``<script type="module">`` widget code runs, so the ``echarts`` global is
    available by the time the module references it — identical ordering to
    the un-inlined external-src version.
    """
    if _ECHARTS_SCRIPT_TAG not in html:
        raise RuntimeError(
            "chart widget HTML no longer loads echarts via "
            "<script src=./vendor/echarts.min.js>; the inliner has nothing "
            "to replace"
        )
    return html.replace(_ECHARTS_SCRIPT_TAG, f"<script>{bundle}</script>")


# Per-filename inlining recipe. A widget HTML is read from disk, then each
# (bundle_filename, splice_fn) pair in its recipe is applied. If a widget
# ever needs no inlining, register it with an empty list.
_RECIPES: dict[str, list[tuple[str, Callable[[str, str], str]]]] = {
    "query_results.html": [("app-with-deps.js", _inline_app_sdk)],
    "chart_results.html": [
        ("echarts.min.js", _inline_echarts),
        ("app-with-deps.js", _inline_app_sdk),
    ],
}


def _read_text(filename: str) -> str:
    """Read a packaged file under ``sqllens.ui`` (HTML at the package root,
    JS bundles under ``vendor/``). Raises the same exceptions the caller's
    error wrapper looks for."""
    pkg = files("sqllens.ui")
    if filename.endswith(".js"):
        return pkg.joinpath("vendor", filename).read_text(encoding="utf-8")
    return pkg.joinpath(filename).read_text(encoding="utf-8")


def _read_widget_html(filename: str) -> str:
    try:
        html = _read_text(filename)
        bundles: dict[str, str] = {}
        for bundle_name, _ in _RECIPES.get(filename, []):
            bundles[bundle_name] = _read_text(bundle_name)
    except (FileNotFoundError, OSError, UnicodeDecodeError, ModuleNotFoundError) as e:
        # A missing asset almost always means the wheel's hatch include globs
        # (see pyproject.toml [tool.hatch.build.targets.wheel].include) dropped
        # one of the files. Surface an actionable message instead of FastMCP's
        # generic resource error, and log server-side so "the widget never
        # renders" is debuggable.
        logger.error(
            "widget asset (%s or one of its vendored bundles) could not be "
            "loaded; the installed wheel is likely missing it — apps-aware "
            "hosts will not render results.",
            filename,
            exc_info=True,
        )
        raise RuntimeError(
            f"result widget asset {filename!r} is unavailable; "
            "reinstall sqllens or check the wheel packaging"
        ) from e
    if not html.strip() or any(not v.strip() for v in bundles.values()):
        # A truncated/empty asset would otherwise be @cache-memoized and render
        # a blank iframe with no diagnostic. Fail with the same actionable error.
        logger.error(
            "widget asset (%s or one of its vendored bundles) is empty; the "
            "installed wheel is likely truncated — apps-aware hosts will not "
            "render results.",
            filename,
        )
        raise RuntimeError(
            f"result widget asset {filename!r} is empty; "
            "reinstall sqllens or check the wheel packaging"
        )
    for bundle_name, splice_fn in _RECIPES.get(filename, []):
        html = splice_fn(html, bundles[bundle_name])
    return html


@cache
def load_widget_html(filename: str = "query_results.html") -> str:
    """Return a packaged widget's HTML source as text, with vendored JS inlined.

    Cached per filename: each asset is immutable in an installed wheel, so a
    process reads it from disk once instead of on every ``ui://`` resource
    fetch. A read failure raises (and is *not* memoized — ``@cache`` only
    stores the successful return), so a transient/packaging fault re-attempts
    on the next fetch rather than poison-caching the exception.
    """
    return _read_widget_html(filename)
