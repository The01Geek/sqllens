# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Secret-safe rendering of pydantic ``ValidationError``.

A ``ValidationError``'s ``str()`` can embed the offending input (bearer token,
API key, DSN password, an oversized SQL string). Render only ``loc``/``msg``
(and optionally ``type``), never ``input``/``ctx``.
"""

from __future__ import annotations

from pydantic import ValidationError


def validation_error_lines(exc: ValidationError, *, with_type: bool) -> list[str]:
    """One ``loc: msg`` (optionally ``[type]``) line per error, no input echoed."""
    lines: list[str] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "")
        prefix = f"{loc}: " if loc else ""
        suffix = f" [{err.get('type', '')}]" if with_type else ""
        lines.append(f"{prefix}{msg}{suffix}")
    return lines
