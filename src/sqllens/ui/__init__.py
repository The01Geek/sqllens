# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Packaged MCP App widget assets for ``query_database``.

The HTML widget and its vendored JS bundle ship inside the wheel (see the
``[tool.hatch.build.targets.wheel].include`` globs in ``pyproject.toml``).
``server.py`` serves :func:`load_widget_html` as the ``ui://`` resource an
apps-aware host renders in a sandboxed iframe.
"""

from __future__ import annotations

from importlib.resources import files


def load_widget_html() -> str:
    """Return the ``query_results.html`` widget source as text."""
    return (
        files("sqllens.ui").joinpath("query_results.html").read_text(encoding="utf-8")
    )
