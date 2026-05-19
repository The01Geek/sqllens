# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Packaged MCP App widget assets for ``query_database``.

The HTML widget and its vendored JS bundle ship inside the wheel (see the
``[tool.hatch.build.targets.wheel].include`` globs in ``pyproject.toml``).
``server.py`` serves :func:`load_widget_html` as the ``ui://`` resource an
apps-aware host renders in a sandboxed iframe.
"""

from __future__ import annotations

import logging
from functools import cache
from importlib.resources import files

logger = logging.getLogger("sqllens.ui")


def _read_widget_html() -> str:
    try:
        html = files("sqllens.ui").joinpath("query_results.html").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError, ModuleNotFoundError) as e:
        # A missing asset almost always means the wheel's hatch include globs
        # (see pyproject.toml [tool.hatch.build.targets.wheel].include) dropped
        # it. Surface an actionable message instead of FastMCP's generic
        # resource error, and log server-side so "the widget never renders" is
        # debuggable.
        logger.error(
            "query_database widget asset (query_results.html) could not be "
            "loaded; the installed wheel is likely missing it — apps-aware "
            "hosts will not render results.",
            exc_info=True,
        )
        raise RuntimeError(
            "query_database result widget asset is unavailable; "
            "reinstall sqllens or check the wheel packaging"
        ) from e
    if not html.strip():
        # A truncated/empty asset would otherwise be @cache-memoized and render
        # a blank iframe with no diagnostic. Fail with the same actionable error.
        logger.error(
            "query_database widget asset (query_results.html) is empty; the "
            "installed wheel is likely truncated — apps-aware hosts will not "
            "render results."
        )
        raise RuntimeError(
            "query_database result widget asset is empty; "
            "reinstall sqllens or check the wheel packaging"
        )
    return html


@cache
def load_widget_html() -> str:
    """Return the ``query_results.html`` widget source as text.

    Cached: the asset is immutable in an installed wheel, so a process reads it
    from disk once instead of on every ``ui://`` resource fetch. A read failure
    raises (and is *not* memoized — ``@cache`` only stores the successful
    return), so a transient/packaging fault re-attempts on the next fetch
    rather than poison-caching the exception.
    """
    return _read_widget_html()
