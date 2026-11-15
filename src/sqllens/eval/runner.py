# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Drive a golden set through the live agent and classify each case.

Verification resolves the persisted default profile, forces SQL details on,
and disables the dedicated agent's memory-writing tools. Missing SQL produces
an ERROR row. Each case has its own conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

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


IdentityField = Literal["rule_set", "account_id", "user_id"]

IDENTITY_LABELS: Mapping[IdentityField, str] = {
    "rule_set": "rule set",
    "account_id": "account ID",
    "user_id": "user ID",
}


class VerificationIdentityError(ValueError):
    """A verification run refused before its first case over its rule-set identity.

    ``kind`` is one of ``no_rule_set_file``, ``missing``, ``account_required``,
    ``refused`` (selection refused; ``detail`` is the ``Request refused: …``
    text) or ``catalog`` (``detail`` names the mismatch or the error that
    stopped the check). ``missing`` names the absent fields. ``rule_set``,
    ``account_id`` and ``user_id`` are the values the run used, and
    ``account_defaulted`` says the account came from the file's only bound ID.
    """

    def __init__(
        self,
        kind: str,
        *,
        detail: str = "",
        missing: tuple[IdentityField, ...] = (),
        rule_set: str | None = None,
        account_id: str | None = None,
        user_id: str | None = None,
        account_defaulted: bool = False,
    ) -> None:
        self.kind = kind
        self.detail = detail
        self.missing = missing
        self.rule_set = rule_set
        self.account_id = account_id
        self.user_id = user_id
        self.account_defaulted = account_defaulted
        super().__init__(self.describe())

    def describe(self, labels: Mapping[IdentityField, str] = IDENTITY_LABELS) -> str:
        """The refusal with each identity field named by ``labels``."""
        names = [labels[field] for field in ("rule_set", "account_id", "user_id")]
        if self.kind == "no_rule_set_file":
            return (
                f"{', '.join(names[:-1])} and {names[-1]} need a [rule_sets] file; "
                "this configuration has none"
            )
        if self.kind == "missing":
            missing = [labels[field] for field in self.missing]
            verb = "is" if len(missing) == 1 else "are"
            return (
                f"a rule-set file is configured, so {' and '.join(missing)} {verb} required"
            )
        if self.kind == "account_required":
            return (
                f"{labels['account_id']} is required because the rule-set file binds "
                "more than one account"
            )
        account = f"{self.account_id!r}"
        if self.account_defaulted:
            account += " (the file's only bound account)"
        return (
            f"{self.detail} ({labels['rule_set']} {self.rule_set!r}, "
            f"{labels['account_id']} {account}, {labels['user_id']} {self.user_id!r})"
        )


async def _verification_metadata(
    cfg: Config,
    rule_set: str | None,
    account_id: str | None,
    user_id: str | None,
) -> dict[str, str] | None:
    """Check the run's identity and confirm the catalog; the ``_meta`` every case carries.

    ``None`` without a rule-set file. Raises ``VerificationIdentityError``.
    """
    loaded = cfg.rule_sets.loaded
    if loaded is None:
        if rule_set is not None or account_id is not None or user_id is not None:
            raise VerificationIdentityError("no_rule_set_file")
        return None
    missing: tuple[IdentityField, ...] = tuple(
        name for name, value in (("rule_set", rule_set), ("user_id", user_id)) if not value
    )
    if missing:
        raise VerificationIdentityError("missing", missing=missing)
    defaulted = not account_id
    if defaulted:
        bound = loaded.file.binding.account_ids
        if len(bound) != 1:
            raise VerificationIdentityError(
                "account_required", rule_set=rule_set, user_id=user_id
            )
        account_id = bound[0]
    identity = {
        "rule_set": rule_set,
        "account_id": account_id,
        "user_id": user_id,
        "account_defaulted": defaulted,
    }
    keys = loaded.file.metadata
    metadata = {keys.rule_set: rule_set, keys.account_id: account_id, keys.user_id: user_id}

    from sqllens.rule_sets import (
        RuleSetCatalogError,
        RuleSetRequestError,
        confirm_catalog,
        select_rule_set,
    )

    try:
        select_rule_set(loaded, metadata)
    except RuleSetRequestError as exc:
        raise VerificationIdentityError("refused", detail=str(exc), **identity) from exc
    try:
        await confirm_catalog(cfg.database.url, loaded)
    except RuleSetCatalogError as exc:
        raise VerificationIdentityError("catalog", detail=str(exc), **identity) from exc
    except Exception as exc:
        raise VerificationIdentityError(
            "catalog",
            detail=f"the rule-set catalog check could not run: {type(exc).__name__}: {exc}",
            **identity,
        ) from exc
    loaded.catalog.confirmed = True
    return metadata


async def run_verification(
    cfg: Config,
    cases: Iterable[GoldenCase],
    *,
    driver: AgentDriver | None = None,
    comparator: Comparator | None = None,
    rule_set: str | None = None,
    user_id: str | None = None,
    account_id: str | None = None,
) -> RunReport:
    """Run each case independently and return its verdict or failure diagnostic.

    ``driver`` replaces the live agent for tests. The production driver builds
    one dedicated agent with no memory-writing tools and never uses the cached
    singleton. A setup failure propagates before any case runs.

    ``comparator`` defaults to dialect-normalized SQL comparison. An async
    replacement accepts expected and actual SQL, returning a ``Status`` or
    raising a diagnostic on failure. It can capture a database connection in a
    closure for row comparison without changing the runner.

    ``rule_set``, ``user_id`` and ``account_id`` are the identity every case
    runs under while a rule-set file is configured. Before any case and any
    agent work, ``VerificationIdentityError`` is raised when: an identity value
    is passed (not ``None``, an empty string included) with no rule-set file;
    ``rule_set`` or ``user_id`` is missing or empty under a rule-set file;
    ``account_id`` is missing or empty and ``binding.account_ids`` lists more
    than one ID (with exactly one, that ID is used); ``select_rule_set``
    refuses the identity; or the catalog check, run once on its own
    connection, fails or cannot run. A passing check marks the catalog
    confirmed. The production driver sends each case's request with the
    identity under the file's ``[metadata]`` key names; a ``driver`` given
    here receives none.
    """
    metadata = await _verification_metadata(cfg, rule_set, account_id, user_id)
    forced_cfg = verification_config(cfg)
    if driver is None:
        from sqllens.agent.factory import build_agent
        from sqllens.tools.query_database import query_database_impl_with_widgets

        verify_agent = build_agent(forced_cfg, allow_memory_writes=False)

        async def _prod_driver(
            forced: Config, question: str
        ) -> tuple[str, list[dict], dict | None, dict | None, dict | None]:
            return await query_database_impl_with_widgets(
                forced, question, metadata=metadata, agent=verify_agent
            )

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
