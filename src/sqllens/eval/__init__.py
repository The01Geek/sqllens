# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Post-import accuracy regression guard for the SQL agent.

A curated *golden set* — operator-authored ``question -> expected-SQL`` pairs —
is run through the live agent. Each case is classified PASS, CHANGED, or ERROR
by normalised-SQL comparison against the configured database dialect. Surfaced
via the ``sqllens verify-memory`` CLI command.

The runner accepts an async comparator for alternative comparison strategies.
"""

from sqllens.eval.compare import Status, compare, normalize_sql
from sqllens.eval.golden import GoldenCase, load_golden
from sqllens.eval.runner import CaseResult, RunReport, run_verification

__all__ = [
    "CaseResult",
    "GoldenCase",
    "RunReport",
    "Status",
    "compare",
    "load_golden",
    "normalize_sql",
    "run_verification",
]
