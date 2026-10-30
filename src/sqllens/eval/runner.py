# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Drive a golden set through the live agent and classify each case.

Verification resolves the persisted default profile, forces SQL details on,
and disables the dedicated agent's memory-writing tools. Missing SQL produces
an ERROR row. Each case has its own conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from sqllens.config import Config
from sqllens.eval.compare import Status, compare_sql
from sqllens.eval.golden import GoldenCase
from sqllens.profiles import ProfileStore, resolve_effective_settings


@dataclass(frozen=True)
class CaseResult:
    """Outcome of one case; every ERROR carries a diagnostic."""

    question: str
    expected_sql: str
    actual_sql: str | None
    status: Status
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status is Status.ERROR:
            if not self.error or not self.error.strip():
                raise ValueError("an ERROR result requires a diagnostic")
        elif self.error is not None or not self.actual_sql or not self.actual_sql.strip():
            raise ValueError("a PASS or CHANGED result requires SQL and no error")


@dataclass(frozen=True)
class RunReport:
    """Immutable aggregate over every case in one verification run."""

    results: tuple[CaseResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status is Status.PASS)

    @property
    def changed(self) -> int:
        return sum(1 for r in self.results if r.status is Status.CHANGED)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status is Status.ERROR)

    @property
    def pass_rate(self) -> float:
        """Fraction of cases that PASSed, in [0.0, 1.0]; zero on an empty run."""
        return self.passed / self.total if self.total else 0.0


# query_info is validated at the driver boundary by _extract_sql.
AgentDriver = Callable[
    [Config, str],
    Awaitable[tuple[str, list[dict], object, dict | None, dict | None]],
]
Comparator = Callable[[str, str], Awaitable[Status]]


def verification_config(cfg: Config) -> Config:
    """Resolve the persisted default profile into a clone, then force verification settings.

    Details are enabled and query saving is disabled without changing the caller
    or the stored profile. The dedicated agent also omits both memory save tools.
    """
    effective = resolve_effective_settings(cfg, None, store=ProfileStore(cfg))
    return cfg.model_copy(
        update={
            "agent": cfg.agent.model_copy(
                update={
                    "show_details": True,
                    "max_tool_iterations": min(
                        cfg.agent.max_tool_iterations, effective.max_tool_iterations
                    ),
                }
            ),
            "database": cfg.database.model_copy(
                update={"max_rows": min(cfg.database.max_rows, effective.max_rows)}
            ),
            "memory": cfg.memory.model_copy(
                update={
                    "save_queries": False,
                    "save_text_memories": False,
                    "similarity_threshold": effective.similarity_threshold,
                }
            ),
        }
    )


async def run_verification(
    cfg: Config,
    cases: Iterable[GoldenCase],
    *,
    driver: AgentDriver | None = None,
    comparator: Comparator | None = None,
) -> RunReport:
    """Run each case independently and return its verdict or failure diagnostic.

    ``driver`` replaces the live agent for tests. The production driver builds
    one dedicated agent with no memory-writing tools and never uses the cached
    singleton. A setup failure propagates before any case runs.

    ``comparator`` defaults to dialect-normalized SQL comparison. An async
    replacement accepts expected and actual SQL, returning a ``Status`` or
    raising a diagnostic on failure. It can capture a database connection in a
    closure for row comparison without changing the runner.
    """
    forced_cfg = verification_config(cfg)
    if driver is None:
        from sqllens.agent.factory import build_agent
        from sqllens.tools.query_database import query_database_impl_with_widgets

        verify_agent = build_agent(forced_cfg, allow_memory_writes=False)

        async def _prod_driver(
            forced: Config, question: str
        ) -> tuple[str, list[dict], dict | None, dict | None, dict | None]:
            return await query_database_impl_with_widgets(forced, question, agent=verify_agent)

        impl: AgentDriver = _prod_driver
    else:
        impl = driver
    if comparator is None:
        async def _sql_comparator(expected: str, actual: str) -> Status:
            return compare_sql(expected, actual, dialect=forced_cfg.database.dialect)

        comparator = _sql_comparator
    results = []
    for case in cases:
        results.append(await _run_one_case(impl, forced_cfg, comparator, case))
    return RunReport(results=tuple(results))


async def _run_one_case(
    driver: AgentDriver,
    forced_cfg: Config,
    comparator: Comparator,
    case: GoldenCase,
) -> CaseResult:
    try:
        _markdown, _blocks, query_info, _memory_info, _trace = await driver(
            forced_cfg, case.question
        )
    except Exception as exc:
        from sqllens.tools.query_database import AgentRunError

        return CaseResult(
            question=case.question,
            expected_sql=case.expected_sql,
            actual_sql=_extract_sql(exc.query_info) if isinstance(exc, AgentRunError) else None,
            status=Status.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
    actual_sql = _extract_sql(query_info)
    if actual_sql is None:
        return CaseResult(
            question=case.question,
            expected_sql=case.expected_sql,
            actual_sql=None,
            status=Status.ERROR,
            error=(
                "agent did not produce a SQL statement (no run_sql tool call, "
                "or show_details was filtered out upstream)"
            ),
        )
    try:
        status = await comparator(case.expected_sql, actual_sql)
    except Exception as exc:
        return CaseResult(
            question=case.question,
            expected_sql=case.expected_sql,
            actual_sql=actual_sql,
            status=Status.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
    return CaseResult(
        question=case.question,
        expected_sql=case.expected_sql,
        actual_sql=actual_sql,
        status=status,
        error="comparator reported an error" if status is Status.ERROR else None,
    )


def _extract_sql(query_info: object) -> str | None:
    """Validate driver metadata and extract its non-blank SQL string."""
    if not isinstance(query_info, dict):
        return None
    sql = query_info.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return None
    return sql
