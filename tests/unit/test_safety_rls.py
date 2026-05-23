# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for row-level security: config validation, the sqlglot AST
rewrite engine, the fail-secure ``RlsGuardRunner``, factory composition, and
the per-request metadata plumbing seams.

The rewrite is exercised against a small representative demo schema
(``customers``, ``orders``) covering single-table, subquery, CTE, join, and
pre-existing-WHERE shapes. Every rewritten statement is asserted to still pass
``assert_select_only`` — the read-only guard runs on the rewritten SQL.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr, ValidationError

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import build_agent
from sqllens.config import (
    RESERVED_METADATA_KEYS,
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    RlsRule,
)
from sqllens.safety import (
    ReadOnlyGuardRunner,
    RlsError,
    RlsGuardRunner,
    apply_rls,
)
from sqllens.safety.readonly import assert_select_only


def _rule(**kw: object) -> RlsRule:
    base: dict[str, object] = {"table": "orders", "column": "tenant_id"}
    base.update(kw)
    return RlsRule(**base)  # type: ignore[arg-type]


# ─────────────────────────── config validation ──────────────────────────────


class TestRlsRuleValidation:
    def test_valid_static_rule(self) -> None:
        r = _rule(value="acme")
        assert r.operator == "="
        assert r.value == "acme"

    def test_valid_dynamic_rule(self) -> None:
        r = _rule(value_from_metadata="tenant_id")
        assert r.value_from_metadata == "tenant_id"

    def test_operator_normalized_to_lowercase(self) -> None:
        assert _rule(value=[1], operator="IN").operator == "in"

    def test_rule_is_frozen(self) -> None:
        # Security-config invariants (XOR value/value_from_metadata, operator
        # allowlist) must hold for the rule's lifetime, not just at load.
        r = _rule(value="acme")
        with pytest.raises(ValidationError):
            r.value_from_metadata = "tenant_id"

    @pytest.mark.parametrize("bad", ["orders; DROP", "ord ers", "1orders", "a.b", '"x"'])
    def test_bad_table_identifier_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="bare SQL identifier"):
            _rule(table=bad, value="x")

    def test_bad_column_identifier_rejected(self) -> None:
        with pytest.raises(ValidationError, match="bare SQL identifier"):
            _rule(column="col-1", value="x")

    def test_unsupported_operator_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not supported"):
            _rule(value="x", operator="LIKE")

    def test_both_value_and_metadata_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            _rule(value="x", value_from_metadata="k")

    def test_neither_value_nor_metadata_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            _rule()

    def test_in_with_non_list_static_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a non-empty list"):
            _rule(value="x", operator="in")

    def test_in_with_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a non-empty list"):
            _rule(value=[], operator="in")

    def test_bad_metadata_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid metadata key"):
            _rule(value_from_metadata="bad key")

    @pytest.mark.parametrize("reserved", sorted(RESERVED_METADATA_KEYS))
    def test_reserved_metadata_key_rejected_at_load(self, reserved: str) -> None:
        # Internal agent-control keys are stripped at the request boundary —
        # a rule that references one would silently block every query against
        # the protected table. Reject the typo at config load instead.
        # Parametrized over RESERVED_METADATA_KEYS itself so a new reserved key
        # is automatically covered, not silently left unrejected.
        with pytest.raises(ValidationError, match="reserved internal agent-control"):
            _rule(value_from_metadata=reserved)

    def test_load_reject_set_matches_boundary_strip_set(self) -> None:
        # The load-time rejection (config) and the request-boundary strip
        # (tools/query_database) MUST enforce the same set, or a key could be
        # load-accepted yet always stripped — the silent-block failure the
        # rejection exists to prevent. Pin them to one source of truth.
        from sqllens.tools.query_database import _RESERVED_METADATA_KEYS

        assert _RESERVED_METADATA_KEYS == RESERVED_METADATA_KEYS

    def test_unknown_key_in_rule_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RlsRule(table="orders", column="t", value="x", colmn="typo")  # type: ignore[call-arg]

    def test_misconfigured_rule_fails_config_load(self, tmp_path: Path) -> None:
        """A bad rule is rejected building the Config (what ``sqllens
        validate`` does), not deferred to query time."""
        with pytest.raises(ValidationError):
            Config(
                database=DatabaseConfig(url="sqlite:///:memory:"),
                llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
                memory=MemoryConfig(persist_dir=tmp_path),
                auth=AuthConfig(mode="none"),
                rls=[{"table": "bad-name", "column": "c", "value": "x"}],  # type: ignore[list-item]
            )


# ─────────────────────────── engine: static rules ───────────────────────────


def _assert_filtered_and_readonly(sql: str, dialect: str = "sqlite") -> None:
    assert_select_only(sql, dialect=dialect)


class TestApplyRlsStatic:
    def test_no_rules_is_passthrough(self) -> None:
        assert apply_rls("SELECT * FROM orders", []) == "SELECT * FROM orders"

    def test_single_table(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" in out and "'acme'" in out
        _assert_filtered_and_readonly(out)

    def test_existing_where_is_and_combined_and_preserved(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders WHERE total > 100",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "total" in out and "100" in out
        assert "tenant_id" in out
        assert " AND " in out.upper()
        _assert_filtered_and_readonly(out)

    def test_join_predicate_qualified_by_alias(self) -> None:
        out = apply_rls(
            "SELECT o.id FROM orders o JOIN customers c ON o.cust = c.id",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        # Predicate must be qualified by the table's alias, not bare.
        assert "o.tenant_id" in out.replace('"', "")
        _assert_filtered_and_readonly(out)

    def test_subquery_scope_is_filtered(self) -> None:
        out = apply_rls(
            "SELECT * FROM (SELECT id FROM orders) sub",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" in out
        _assert_filtered_and_readonly(out)

    def test_cte_body_filtered_not_cte_reference(self) -> None:
        out = apply_rls(
            "WITH scoped AS (SELECT id FROM orders) SELECT * FROM scoped",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        # Exactly one predicate: inside the CTE body. The outer `FROM scoped`
        # references the CTE alias (not a base table) and must NOT be filtered.
        assert out.count("tenant_id") == 1
        _assert_filtered_and_readonly(out)

    def test_every_matching_reference_filtered(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders UNION SELECT id FROM orders",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert out.count("tenant_id") == 2
        _assert_filtered_and_readonly(out)

    def test_unrelated_table_untouched(self) -> None:
        out = apply_rls(
            "SELECT * FROM customers",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" not in out

    def test_static_in_operator(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders",
            [_rule(value=["a", "b"], operator="in")],
            dialect="sqlite",
        )
        assert "IN" in out.upper() and "'a'" in out and "'b'" in out
        _assert_filtered_and_readonly(out)

    def test_numeric_value(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders",
            [_rule(column="region_id", value=7)],
            dialect="sqlite",
        )
        assert "region_id" in out and "7" in out
        _assert_filtered_and_readonly(out)


# ───────────────── engine: every-scope coverage + fail-secure shapes ─────────


class TestApplyRlsScopeCoverage:
    """The acceptance criterion is 'filters every matching table reference'.

    These exercise scopes the naive FROM/JOIN walk missed, and assert the
    fail-secure backstop blocks (rather than silently passing) any protected
    table reference the rewrite cannot prove scoped.
    """

    def test_cte_alias_colliding_with_protected_table_filters_body(self) -> None:
        # Regression: a CTE named like a protected base table previously made
        # the global cte-name set skip the real base-table read inside the CTE
        # body — an unfiltered query. The body must now be filtered.
        out = apply_rls(
            "WITH orders AS (SELECT id FROM orders) SELECT * FROM orders",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        # Exactly one predicate: inside the CTE body (the real base table).
        # The outer `FROM orders` is the CTE reference and must not be filtered.
        assert out.count("tenant_id") == 1
        _assert_filtered_and_readonly(out)

    def test_recursive_cte_colliding_name_filters_body(self) -> None:
        out = apply_rls(
            "WITH RECURSIVE orders AS (SELECT id FROM orders) "
            "SELECT * FROM orders",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert out.count("tenant_id") == 1
        _assert_filtered_and_readonly(out)

    def test_where_in_subquery_is_filtered(self) -> None:
        out = apply_rls(
            "SELECT * FROM customers WHERE id IN (SELECT cust FROM orders)",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert out.count("tenant_id") == 1
        _assert_filtered_and_readonly(out)

    def test_comma_join_is_filtered(self) -> None:
        out = apply_rls(
            "SELECT * FROM orders, customers",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" in out
        _assert_filtered_and_readonly(out)

    def test_schema_qualified_table_is_filtered(self) -> None:
        out = apply_rls(
            "SELECT * FROM public.orders",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" in out
        _assert_filtered_and_readonly(out)

    def test_parenthesized_table_source_is_filtered(self) -> None:
        out = apply_rls(
            "SELECT * FROM (orders)",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        assert "tenant_id" in out
        _assert_filtered_and_readonly(out)

    def test_unprovable_shape_blocks_fail_secure(self) -> None:
        # `(orders) o` aliases a parenthesized bare table: there is no SELECT
        # scope to attach a WHERE to, so the rewrite cannot prove the read is
        # scoped. Fail-secure: block, never pass it through unfiltered.
        with pytest.raises(RlsError, match="could not prove"):
            apply_rls(
                "SELECT * FROM (orders) o",
                [_rule(value="acme")],
                dialect="sqlite",
            )

    def test_table_command_blocks_fail_secure(self) -> None:
        # Postgres `TABLE orders` is semantically `SELECT * FROM orders` but
        # parses to a non-Query root with no exp.Table node — the scope walk
        # and backstop would never see the protected read. Fail-secure: block.
        # Case folding of the keyword and the alternate orientation behave the
        # same way through this gate.
        for stmt in ("TABLE orders", "table orders"):
            with pytest.raises(RlsError, match="SELECT-shaped"):
                apply_rls(stmt, [_rule(value="acme")], dialect="postgres")

    @pytest.mark.parametrize("dialect", ["postgres", "sqlite", "mysql"])
    def test_table_subquery_misparse_blocks_fail_secure(self, dialect: str) -> None:
        # `(TABLE orders) sub` is misparsed by sqlglot for every supported
        # dialect: the keyword ``TABLE`` becomes the table name and the
        # protected name ``orders`` lands in the .alias slot. A .name-only
        # backstop misses it; the alias check must catch it and block.
        for stmt in (
            "SELECT * FROM (TABLE orders) sub",
            "SELECT * FROM LATERAL (TABLE orders) sub",
        ):
            with pytest.raises(RlsError, match="could not prove"):
                apply_rls(stmt, [_rule(value="acme")], dialect=dialect)

    def test_recursive_cte_named_like_table_does_not_read_base_table(
        self,
    ) -> None:
        # A recursive CTE named `orders` shadows base table `orders` for the
        # whole WITH scope (standard SQL): every `orders` here is the working
        # table, not the base table, so there is no protected read to filter
        # and the query is correctly emitted unchanged (not a fail-open).
        sql = (
            "WITH RECURSIVE orders AS (SELECT 1 AS id UNION "
            "SELECT id FROM orders) SELECT * FROM orders"
        )
        out = apply_rls(sql, [_rule(value="acme")], dialect="sqlite")
        assert "tenant_id" not in out
        _assert_filtered_and_readonly(out)

    def test_recursive_cte_distinct_name_filters_base_table(self) -> None:
        # When the recursive CTE has its own name, the real base-table reads
        # inside its anchor and recursive term ARE filtered.
        sql = (
            "WITH RECURSIVE tree AS (SELECT id FROM orders UNION "
            "SELECT o.id FROM orders o JOIN tree t ON o.parent = t.id) "
            "SELECT * FROM tree"
        )
        out = apply_rls(sql, [_rule(value="acme")], dialect="sqlite")
        assert out.count("tenant_id") == 2
        _assert_filtered_and_readonly(out)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM (SELECT 1) AS orders, orders",
            "SELECT * FROM (SELECT 1) AS orders CROSS JOIN orders",
            "SELECT * FROM (SELECT 1) AS orders LEFT JOIN orders ON 1=1",
        ],
    )
    def test_derived_alias_colliding_with_sibling_base_table(
        self, sql: str
    ) -> None:
        # A derived subquery aliased with a protected name and a sibling base
        # reference to that same protected name produces a scope.sources key
        # collision; sqlglot renames the base-table source to <name>_2. A
        # lookup by alias_or_name would silently miss the renamed source and
        # classify the real base read as a CTE reference (a fail-open). The
        # rewrite must scan sources by value, not by key, so the renamed base
        # source is still injected.
        out = apply_rls(sql, [_rule(value="acme")], dialect="sqlite")
        assert "tenant_id" in out
        _assert_filtered_and_readonly(out)

    def test_mixed_real_base_and_cte_reference(self) -> None:
        out = apply_rls(
            "WITH scoped AS (SELECT id FROM orders) "
            "SELECT * FROM scoped JOIN orders ON scoped.id = orders.id",
            [_rule(value="acme")],
            dialect="sqlite",
        )
        # One predicate in the CTE body, one for the real base-table join.
        assert out.count("tenant_id") == 2
        _assert_filtered_and_readonly(out)


# ─────────────────────────── engine: dynamic + fail-secure ──────────────────


class TestApplyRlsDynamicFailSecure:
    def test_dynamic_value_present(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders",
            [_rule(value_from_metadata="tenant_id")],
            dialect="sqlite",
            metadata={"tenant_id": "acme"},
        )
        assert "'acme'" in out
        _assert_filtered_and_readonly(out)

    def test_missing_dynamic_value_blocks(self) -> None:
        with pytest.raises(RlsError, match="not supplied"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tenant_id")],
                dialect="sqlite",
                metadata={},
            )

    @pytest.mark.parametrize(
        "bad",
        ["", "tab\tnull", "x\x00y", "newline\ninjection", "a" * 5000],
    )
    def test_suspicious_string_value_blocks(self, bad: str) -> None:
        with pytest.raises(RlsError, match="suspicious"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tenant_id")],
                dialect="sqlite",
                metadata={"tenant_id": bad},
            )

    @pytest.mark.parametrize("bad", ["", "tab\tnull", "x\x00y"])
    def test_suspicious_item_in_dynamic_in_list_blocks(self, bad: str) -> None:
        # The per-item suspicious-scalar check on the 'in' path is a distinct
        # call site from the scalar path; an empty/control-char value smuggled
        # into a metadata list must block just the same.
        with pytest.raises(RlsError, match="suspicious"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tids", operator="in")],
                dialect="sqlite",
                metadata={"tids": ["acme", bad]},
            )

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_dynamic_value_blocks(self, bad: bool) -> None:
        # isinstance(True, int) is true in Python: an unguarded int branch
        # would accept a metadata-supplied bool and convert it to a TRUE/1
        # literal, coercing ``tenant_id = <token>`` into ``tenant_id = 1``.
        # An identity token is never a boolean — fail-secure and block.
        with pytest.raises(RlsError, match="suspicious"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tenant_id")],
                dialect="sqlite",
                metadata={"tenant_id": bad},
            )

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_item_in_dynamic_in_list_blocks(self, bad: bool) -> None:
        # The 'in'-list path routes each element through the same
        # _is_suspicious_scalar helper as the scalar path, so a smuggled bool
        # must block there too — not coerce to a TRUE/1 literal in the IN-set.
        with pytest.raises(RlsError, match="suspicious"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tids", operator="in")],
                dialect="sqlite",
                metadata={"tids": ["acme", bad]},
            )

    @pytest.mark.parametrize("bad", [None, {"a": 1}, b"bytes", ["nested"]])
    def test_wrong_typed_dynamic_value_blocks(self, bad: object) -> None:
        with pytest.raises(RlsError):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tenant_id")],
                dialect="sqlite",
                metadata={"tenant_id": bad},
            )

    def test_dynamic_in_requires_nonempty_list(self) -> None:
        with pytest.raises(RlsError, match="non-empty list"):
            apply_rls(
                "SELECT id FROM orders",
                [_rule(value_from_metadata="tids", operator="in")],
                dialect="sqlite",
                metadata={"tids": "not-a-list"},
            )

    def test_dynamic_in_present(self) -> None:
        out = apply_rls(
            "SELECT id FROM orders",
            [_rule(value_from_metadata="tids", operator="in")],
            dialect="sqlite",
            metadata={"tids": ["a", "b"]},
        )
        assert "IN" in out.upper() and "'a'" in out
        _assert_filtered_and_readonly(out)

    def test_parse_failure_blocks(self) -> None:
        with pytest.raises(RlsError, match="could not parse"):
            apply_rls(
                "SELECT FROM WHERE ((",
                [_rule(value="acme")],
                dialect="sqlite",
            )

    def test_multiple_statements_blocks(self) -> None:
        with pytest.raises(RlsError, match="exactly one"):
            apply_rls(
                "SELECT 1; SELECT 2",
                [_rule(value="acme")],
                dialect="sqlite",
            )


# ─────────────────────────── RlsGuardRunner ─────────────────────────────────


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[RunSqlToolArgs] = []

    async def run_sql(
        self, args: RunSqlToolArgs, context: object
    ) -> pd.DataFrame:
        self.calls.append(args)
        return pd.DataFrame()


def _ctx(metadata: dict | None = None) -> ToolContext:
    return ToolContext.model_construct(metadata=metadata or {})


class TestRlsGuardRunner:
    @pytest.mark.asyncio
    async def test_rewritten_sql_reaches_inner_runner(self) -> None:
        inner = _RecordingRunner()
        guard = RlsGuardRunner(inner, [_rule(value="acme")], dialect="sqlite")
        await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM orders"), _ctx())
        assert len(inner.calls) == 1
        assert "tenant_id" in inner.calls[0].sql

    @pytest.mark.asyncio
    async def test_missing_dynamic_value_blocks_before_inner(self) -> None:
        inner = _RecordingRunner()
        guard = RlsGuardRunner(
            inner, [_rule(value_from_metadata="tenant_id")], dialect="sqlite"
        )
        with pytest.raises(RlsError, match="refusing to execute"):
            await guard.run_sql(
                RunSqlToolArgs(sql="SELECT id FROM orders"), _ctx({})
            )
        assert inner.calls == []

    @pytest.mark.asyncio
    async def test_dynamic_value_from_context_metadata(self) -> None:
        inner = _RecordingRunner()
        guard = RlsGuardRunner(
            inner, [_rule(value_from_metadata="tenant_id")], dialect="sqlite"
        )
        await guard.run_sql(
            RunSqlToolArgs(sql="SELECT id FROM orders"),
            _ctx({"tenant_id": "acme"}),
        )
        assert "'acme'" in inner.calls[0].sql

    @pytest.mark.asyncio
    async def test_unexpected_error_fails_closed(self, monkeypatch) -> None:
        inner = _RecordingRunner()
        guard = RlsGuardRunner(inner, [_rule(value="acme")], dialect="sqlite")

        def _boom(*a: object, **k: object) -> str:
            raise RuntimeError("kaboom")

        monkeypatch.setattr("sqllens.safety.apply_rls", _boom)
        with pytest.raises(RlsError, match="guard errored"):
            await guard.run_sql(RunSqlToolArgs(sql="SELECT 1"), _ctx())
        assert inner.calls == []

    @pytest.mark.asyncio
    async def test_static_rule_works_with_empty_metadata(self) -> None:
        """Static rules need no per-request channel — enforced on stdio too."""
        inner = _RecordingRunner()
        guard = RlsGuardRunner(inner, [_rule(value="acme")], dialect="sqlite")
        await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM orders"), _ctx())
        assert "'acme'" in inner.calls[0].sql


# ─────────────────────────── factory composition ────────────────────────────


def _cfg(tmp_path: Path, rls: list[RlsRule]) -> Config:
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
        rls=rls,
    )


def _unwrap(tool: object) -> object:
    return getattr(tool, "_wrapped_tool", tool)


class TestFactoryComposition:
    def test_rls_guard_outermost_ahead_of_readonly(self, tmp_path: Path) -> None:
        agent = build_agent(_cfg(tmp_path, [_rule(value="acme")]))
        runner = _unwrap(agent.tool_registry._tools["run_sql"]).sql_runner
        assert isinstance(runner, RlsGuardRunner)
        # Read-only guard validates the *rewritten* SQL → it sits inside RLS.
        assert isinstance(runner._inner, ReadOnlyGuardRunner)

    def test_no_rls_guard_when_no_rules(self, tmp_path: Path) -> None:
        agent = build_agent(_cfg(tmp_path, []))
        runner = _unwrap(agent.tool_registry._tools["run_sql"]).sql_runner

        def _walk(r: object) -> bool:
            while r is not None:
                if isinstance(r, RlsGuardRunner):
                    return True
                r = getattr(r, "_inner", None)
            return False

        assert not _walk(runner)


# ─────────────────────────── metadata plumbing seams ────────────────────────


class TestMetadataPlumbing:
    @pytest.mark.asyncio
    async def test_query_database_forwards_metadata_to_request_context(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``query_database_impl`` must put caller metadata onto the
        ``RequestContext`` it hands the agent."""
        from sqllens.tools import query_database as qd

        seen: dict = {}

        class _RecordingAgent:
            def send_message(self, request_context, message, *, conversation_id=None):
                seen["metadata"] = dict(request_context.metadata)

                async def _gen():
                    if False:
                        yield  # pragma: no cover

                return _gen()

        async def _fake_agent_for(_cfg: Config):
            return _RecordingAgent()

        monkeypatch.setattr(qd, "get_agent", _fake_agent_for)
        cfg = _cfg(tmp_path, [])
        await qd.query_database_impl(cfg, "q", metadata={"tenant_id": "acme"})
        assert seen["metadata"] == {"tenant_id": "acme"}

    @pytest.mark.asyncio
    async def test_reserved_metadata_keys_stripped_from_request_context(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Caller-supplied metadata must not be able to steer internal agent
        control flow (``starter_ui_request`` / ``ui_features_available``);
        those keys are stripped at the boundary, RLS keys pass through."""
        from sqllens.tools import query_database as qd

        seen: dict = {}

        class _RecordingAgent:
            def send_message(self, request_context, message, *, conversation_id=None):
                seen["metadata"] = dict(request_context.metadata)

                async def _gen():
                    if False:
                        yield  # pragma: no cover

                return _gen()

        async def _fake_agent_for(_cfg: Config):
            return _RecordingAgent()

        monkeypatch.setattr(qd, "get_agent", _fake_agent_for)
        cfg = _cfg(tmp_path, [])
        await qd.query_database_impl(
            cfg,
            "q",
            metadata={
                "tenant_id": "acme",
                "starter_ui_request": True,
                "ui_features_available": ["evil"],
            },
        )
        assert seen["metadata"] == {"tenant_id": "acme"}

    def test_request_metadata_extracts_meta_extras(self) -> None:
        from sqllens.server import _request_metadata

        class _Meta:
            model_extra: dict = {"tenant_id": "acme"}  # noqa: RUF012

        class _RC:
            meta = _Meta()

        class _Ctx:
            request_context = _RC()

        assert _request_metadata(_Ctx()) == {"tenant_id": "acme"}

    def test_request_metadata_failsafe_on_no_request(self) -> None:
        from sqllens.server import _request_metadata

        class _Ctx:
            @property
            def request_context(self) -> object:
                raise ValueError("not in a request")

        assert _request_metadata(_Ctx()) == {}

    def test_request_metadata_handles_none_meta(self) -> None:
        from sqllens.server import _request_metadata

        class _RC:
            meta = None

        class _Ctx:
            request_context = _RC()

        assert _request_metadata(_Ctx()) == {}
