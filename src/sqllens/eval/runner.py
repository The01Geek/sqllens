# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Drive a golden set through the live agent and classify each case.

The runner is intentionally thin: it forces ``show_details`` on (so
``query_info["sql"]`` is populated for every case), invokes the same agent
path the ``query_database`` MCP tool uses, extracts the agent's generated SQL,
and hands the ``(expected, actual)`` pair to the pluggable comparator in
:mod:`sqllens.eval.compare` for the verdict.

The runner deliberately does **not** know the PASS / CHANGED / ERROR taxonomy.
A future row-execution comparator (execute both queries against the configured
database, compare result sets) drops in by satisfying the same comparator
contract — no runner changes required.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from sqllens.config import Config
from sqllens.eval.compare import Status, compare
from sqllens.eval.golden import GoldenCase


@dataclass(frozen=True)
class CaseResult:
    """Outcome of running one golden case through the agent."""

    question: str
    expected_sql: str
    # The agent's generated SQL — ``None`` when the agent raised before
    # producing one (a tool-internal failure, an LLM error, etc.).
    actual_sql: str | None
    status: Status
    # Populated when the agent path raised; ``None`` otherwise. Surfaced in the
    # CLI's per-case table for any non-PASS row.
    error: str | None = None


@dataclass(frozen=True)
class RunReport:
    """Aggregate over every :class:`CaseResult` in one verification run."""

    results: list[CaseResult] = field(default_factory=list)

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
        """Fraction of cases that PASSed, in ``[0.0, 1.0]``. Zero on an empty run.

        An empty run is itself a CLAUDE.md "structured signal" failure mode —
        the CLI guards against ever reporting it as a green result — so the
        zero return here is the "no signal, no credit" floor, not a value any
        downstream gate would compare against meaningfully.
        """
        if self.total == 0:
            return 0.0
        return self.passed / self.total


# Type alias for the agent-driver injection seam — a coroutine
# ``(cfg, question) -> (markdown, blocks, query_info, memory_info, agent_trace)``.
# Tests inject a deterministic fake; production wires
# :func:`sqllens.tools.query_database.query_database_impl_with_widgets`.
AgentDriver = Callable[
    [Config, str],
    Awaitable[tuple[str, list[dict], dict | None, dict | None, dict | None]],
]


async def run_verification(
    cfg: Config,
    cases: Iterable[GoldenCase],
    *,
    driver: AgentDriver | None = None,
) -> RunReport:
    """Run every ``cases`` entry through the agent and classify the result.

    Each case is run independently — one agent run per case, no shared
    conversation. The runner extracts ``query_info["sql"]`` from the
    ``show_details``-on response and hands it to :func:`compare` along with
    the configured database dialect (``cfg.database.dialect``). A case where
    the agent raises (any exception, including a sanitized tool-internal
    failure) classifies as :class:`Status.ERROR` with the exception message
    surfaced on :attr:`CaseResult.error` and ``actual_sql=None``.

    A case where the agent answered but ``query_info["sql"]`` came back
    missing — meaning the show-details force was bypassed by an upstream
    change, or no ``run_sql`` tool call happened — also classifies as ERROR
    with a clear diagnostic message rather than being silently scored against
    an empty string.

    The runner clones ``cfg`` with ``agent.show_details=True`` rather than
    mutating it: a concurrent request path that holds the same ``cfg``
    reference must see no change. This relies on the runner never passing a
    ``profile=`` arg to the agent driver — if it ever did, the per-request
    profile resolution would shadow the cloned base. The single call site
    here makes that explicit; revisit if verify-memory grows profile awareness.

    ``driver`` is the injection seam for tests; production callers leave it
    ``None`` and the runner wires :func:`query_database_impl_with_widgets`.
    """
    if driver is None:
        from sqllens.tools.query_database import query_database_impl_with_widgets

        async def _prod_driver(
            forced: Config, question: str
        ) -> tuple[str, list[dict], dict | None, dict | None, dict | None]:
            return await query_database_impl_with_widgets(forced, question)

        impl: AgentDriver = _prod_driver
    else:
        impl = driver
    forced_cfg = cfg.model_copy(
        update={"agent": cfg.agent.model_copy(update={"show_details": True})}
    )
    dialect = forced_cfg.database.dialect
    results: list[CaseResult] = []
    for case in cases:
        results.append(
            await _run_one_case(impl, forced_cfg, dialect, case)
        )
    return RunReport(results=results)


async def _run_one_case(
    driver: AgentDriver,
    forced_cfg: Config,
    dialect: str,
    case: GoldenCase,
) -> CaseResult:
    try:
        _markdown, _blocks, query_info, _memory_info, _trace = await driver(
            forced_cfg, case.question
        )
    except Exception as exc:
        return CaseResult(
            question=case.question,
            expected_sql=case.expected_sql,
            actual_sql=None,
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
    status = compare(case.expected_sql, actual_sql, dialect=dialect)
    return CaseResult(
        question=case.question,
        expected_sql=case.expected_sql,
        actual_sql=actual_sql,
        status=status,
    )


def _extract_sql(query_info: dict | None) -> str | None:
    """Pull the executed SQL out of ``query_info`` if present and non-empty.

    Matches the shape ``query_database_impl_with_widgets`` puts on the wire:
    ``query_info`` is either ``None`` (show-details off — defensive: this
    runner forces it on, but a future filter could still null it) or a mapping
    with a ``"sql"`` key. An empty/whitespace string is treated as missing so
    the comparator never normalises an empty input.
    """
    if query_info is None:
        return None
    sql = query_info.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return None
    return sql
