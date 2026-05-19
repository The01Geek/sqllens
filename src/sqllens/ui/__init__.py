# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Packaged MCP App widget assets for ``query_database``.

The HTML widget and its vendored JS bundle ship inside the wheel (see the
``[tool.hatch.build.targets.wheel].include`` globs in ``pyproject.toml``).
``server.py`` serves :func:`load_widget_html` as the ``ui://`` resource an
apps-aware host renders in a sandboxed iframe.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load_widget_html() -> str:
    """Return the ``query_results.html`` widget source as text.

    Cached: the asset is immutable in an installed wheel, so a process reads it
    from disk once instead of on every ``ui://`` resource fetch.
    """
    return files("sqllens.ui").joinpath("query_results.html").read_text(encoding="utf-8")
