# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Per-role rule sets: the version-1 rule-set file, request selection and catalog check.

A rule-set file names, for one MySQL database, which tables and columns each
application role may read, and which rows of a table through its optional
``row_rule``. ``Config`` loads and validates the file named by
``[rule_sets] file`` and compiles each row rule (see :func:`load_rule_sets`);
``query_database`` selects one set per request from three ``_meta`` keys
(:func:`select_rule_set`); and :func:`check_catalog` confirms that every table
and column the file names exists in the connected database with a type its
row rules can compare. The SQL guard that enforces a selected set lives in
:mod:`sqllens.safety.rule_sets`.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime
import hashlib
import json
import logging
import math
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlglot import expressions as exp

from sqllens._memory_groups import GroupName
from sqllens.config import RESERVED_METADATA_KEYS

logger = logging.getLogger("sqllens.rule_sets")

_ID_PATTERN = r"^(0|[1-9][0-9]*)$"
_IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

RuleSetId = Annotated[StrictStr, StringConstraints(pattern=_ID_PATTERN)]
Identifier = Annotated[StrictStr, StringConstraints(pattern=_IDENTIFIER_PATTERN)]
NonEmptyStr = Annotated[StrictStr, StringConstraints(min_length=1)]

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")


def _no_duplicates(values: list[Any], what: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what} {value!r}")
        seen.add(value)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Binding(_Record):
    """The database and the application accounts a rule-set file is bound to."""

    database: Identifier
    account_ids: list[RuleSetId] = Field(min_length=1)

    @field_validator("account_ids")
    @classmethod
    def _unique_accounts(cls, value: list[str]) -> list[str]:
        _no_duplicates(value, "account ID")
        return value


class MetadataKeys(_Record):
    """The three request ``_meta`` keys that carry a request's identity."""

    rule_set: Identifier
    account_id: Identifier
    user_id: Identifier

    @model_validator(mode="after")
    def _distinct_unreserved(self) -> MetadataKeys:
        keys = [self.rule_set, self.account_id, self.user_id]
        if len(set(keys)) != len(keys):
            raise ValueError("the three metadata key names must be distinct")
        for key in keys:
            if key in RESERVED_METADATA_KEYS:
                raise ValueError(
                    f"metadata key name {key!r} is reserved by SQL Lens; reserved keys: "
                    + ", ".join(sorted(RESERVED_METADATA_KEYS))
                )
        return self


class GlobalSwitch(_Record):
    name: Identifier
    value: StrictBool


class Source(_Record):
    """Provenance of a rule set.

    Validated. ``query_database`` never sends it to the model;
    ``get_access_rules`` returns it.
    """

    confirmed_against_production: StrictBool
    role_id: RuleSetId | None = None
    role_name: NonEmptyStr | None = None
    role_copy: NonEmptyStr | None = None
    permission_list_version: StrictInt | None = Field(default=None, ge=0)
    global_switches: list[GlobalSwitch] | None = None
    permission_fingerprint: StrictStr | None = None
    application_commit: StrictStr | None = None
    generated_on: StrictStr | None = None

    @field_validator("global_switches")
    @classmethod
    def _unique_switches(cls, value: list[GlobalSwitch] | None) -> list[GlobalSwitch] | None:
        if value is not None:
            _no_duplicates([switch.name for switch in value], "global switch name")
        return value

    @field_validator("permission_fingerprint")
    @classmethod
    def _fingerprint(cls, value: str | None) -> str | None:
        if value is not None and not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("must be 'sha256:' followed by 64 lowercase hexadecimal digits")
        return value

    @field_validator("application_commit")
    @classmethod
    def _commit(cls, value: str | None) -> str | None:
        if value is not None and not _COMMIT_RE.fullmatch(value):
            raise ValueError("must be 40 or 64 lowercase hexadecimal digits")
        return value

    @field_validator("generated_on")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _DATE_RE.fullmatch(value):
            raise ValueError("must be an ISO calendar date string YYYY-MM-DD")
        try:
            datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("must be an ISO calendar date string YYYY-MM-DD") from exc
        return value


# --- row rules ----------------------------------------------------------------

ValueType = Literal["integer", "float", "string", "boolean"]
Operator = Literal["=", "!=", "<", "<=", ">", ">=", "in"]

# MySQL ``INFORMATION_SCHEMA.COLUMNS.DATA_TYPE`` values each ``value_type`` may
# compare. A type added here lets MySQL convert one side of a comparison
# (a string column compared with a number reads '1a' as 1), which can show a
# row the rule hides.
VALUE_TYPE_DATA_TYPES: Mapping[str, frozenset[str]] = MappingProxyType({
    "integer": frozenset({"tinyint", "smallint", "mediumint", "int", "bigint"}),
    "float": frozenset({"float", "double"}),
    "string": frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum"}),
    "boolean": frozenset({"tinyint", "smallint", "mediumint", "int", "bigint"}),
})

_SCALAR_TYPES: Mapping[str, type] = MappingProxyType(
    {"integer": int, "float": float, "string": str, "boolean": bool}
)


def _check_scalar(value: object, value_type: str) -> None:
    # ``type() is``: a TOML Boolean is a Python bool, which isinstance counts as an int.
    if type(value) is not _SCALAR_TYPES[value_type]:
        raise ValueError(f"value {value!r} is not a TOML {value_type}")
    if value_type == "float" and not math.isfinite(value):  # type: ignore[arg-type]
        raise ValueError(f"value {value!r} is not a finite float")


class CompareRule(_Record):
    """Visible when the row's ``column`` compares true with a static value or the user ID."""

    kind: Literal["compare"]
    column: Identifier
    operator: Operator
    value_type: ValueType
    value: Any = None
    request: StrictStr | None = None

    @model_validator(mode="after")
    def _operand(self) -> CompareRule:
        has_value = "value" in self.model_fields_set
        if has_value == (self.request is not None):
            raise ValueError("compare needs exactly one of value or request")
        if self.value_type == "boolean" and self.operator not in ("=", "!=", "in"):
            raise ValueError(f"operator {self.operator!r} cannot compare a boolean")
        if self.request is not None:
            if self.request != "user_id":
                raise ValueError(f"request {self.request!r} is not 'user_id'")
            if self.value_type != "integer":
                raise ValueError("request = 'user_id' needs value_type = 'integer'")
            if self.operator == "in":
                raise ValueError("request = 'user_id' cannot use operator 'in'")
            return self
        if self.operator == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("operator 'in' needs a non-empty array value")
            for item in self.value:
                _check_scalar(item, self.value_type)
        else:
            _check_scalar(self.value, self.value_type)
        return self


class AnyOfRule(_Record):
    """Visible when at least one child rule is true."""

    kind: Literal["any_of"]
    rules: list[RowRule] = Field(min_length=1)


class AllOfRule(_Record):
    """Visible when every child rule is true."""

    kind: Literal["all_of"]
    rules: list[RowRule] = Field(min_length=1)


class OwnerViaRule(_Record):
    """Visible when a ``link_table`` row links the row's ``id_column`` to the request's user."""

    kind: Literal["owner_via"]
    id_column: Identifier
    link_table: Identifier
    link_row_column: Identifier
    link_user_column: Identifier


class IsNullRule(_Record):
    """Visible when the row's ``column`` is NULL."""

    kind: Literal["is_null"]
    column: Identifier


class IsNotNullRule(_Record):
    """Visible when the row's ``column`` is not NULL."""

    kind: Literal["is_not_null"]
    column: Identifier


class InheritRule(_Record):
    """Visible when a visible ``parent_table`` row has ``parent_column`` equal to ``column``.

    With ``parent_rule``, that same parent row must also pass ``parent_rule``,
    a column-local node over the parent table's declared columns.
    """

    kind: Literal["inherit"]
    column: Identifier
    parent_table: Identifier
    parent_column: Identifier
    parent_rule: RowRule | None = None


class ReferencedByLink(_Record):
    table: Identifier
    source_column: Identifier
    target_column: Identifier


class ReferencedByRule(_Record):
    """Visible when a visible ``source_table`` row references the row, directly or by a link."""

    kind: Literal["referenced_by"]
    id_column: Identifier
    source_table: Identifier
    source_column: Identifier | None = None
    source_id_column: Identifier | None = None
    link: ReferencedByLink | None = None

    @model_validator(mode="after")
    def _variant(self) -> ReferencedByRule:
        direct = self.source_column is not None
        linked = self.source_id_column is not None or self.link is not None
        if direct == linked:
            raise ValueError(
                "referenced_by needs exactly one variant: source_column, or "
                "source_id_column with link"
            )
        if linked and (self.source_id_column is None or self.link is None):
            raise ValueError("referenced_by through a link needs both source_id_column and link")
        return self


RowRule = Annotated[
    CompareRule
    | IsNullRule
    | IsNotNullRule
    | AnyOfRule
    | AllOfRule
    | OwnerViaRule
    | InheritRule
    | ReferencedByRule,
    Field(discriminator="kind"),
]

AnyOfRule.model_rebuild()
AllOfRule.model_rebuild()
InheritRule.model_rebuild()

_COLUMN_LOCAL = (CompareRule, IsNullRule, IsNotNullRule, AnyOfRule, AllOfRule)
# The ``kind`` of each ``_COLUMN_LOCAL`` record, in the same order, for refusal messages.
_COLUMN_LOCAL_KINDS = "compare, is_null, is_not_null, any_of, all_of"


def _rule_nodes(rule: RowRule, path: str) -> Iterator[tuple[RowRule, str]]:
    """Every node of ``rule`` except ``parent_rule`` subtrees, with its record path.

    Walked iteratively, children after parents. An ``inherit`` node's
    ``parent_rule`` is deliberately not walked: its nodes read the parent
    table, not the table ``rule`` belongs to, so a caller that needs them
    walks ``node.parent_rule`` itself.
    """
    stack = [(rule, path)]
    while stack:
        node, node_path = stack.pop()
        yield node, node_path
        if isinstance(node, AnyOfRule | AllOfRule):
            children = list(enumerate(node.rules))
            stack.extend((child, f"{node_path}.rules[{i}]") for i, child in reversed(children))


def _rule_edges(rule: RowRule, listed: set[str]) -> list[str]:
    """The tables whose own row rule ``rule`` depends on.

    ``owner_via``, ``parent_rule`` and a ``referenced_by`` link table that is
    not in ``listed`` add none.
    """
    edges: list[str] = []
    for node, _ in _rule_nodes(rule, ""):
        if isinstance(node, InheritRule):
            edges.append(node.parent_table)
        elif isinstance(node, ReferencedByRule):
            edges.append(node.source_table)
            if node.link is not None and node.link.table in listed:
                edges.append(node.link.table)
    return edges


class TableRule(_Record):
    """One table a rule set lets a request read, its readable columns and its row rule."""

    name: Identifier
    columns: list[Identifier] = Field(min_length=1)
    predicate_columns: list[Identifier] = Field(default_factory=list)
    row_rule: RowRule | None = None

    @model_validator(mode="after")
    def _columns(self) -> TableRule:
        _no_duplicates(self.columns, "column")
        _no_duplicates(self.predicate_columns, "predicate column")
        overlap = sorted(set(self.columns) & set(self.predicate_columns))
        if overlap:
            raise ValueError(f"predicate column {overlap[0]!r} is also listed in columns")
        return self


class RuleSet(_Record):
    name: RuleSetId
    scope: NonEmptyStr
    source: Source
    tables: list[TableRule] = Field(min_length=1)
    # The memory groups requests under this set may read, besides untagged memory.
    memory_groups: list[GroupName] | None = Field(default=None, min_length=1)

    @field_validator("memory_groups")
    @classmethod
    def _unique_memory_groups(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            _no_duplicates(value, "memory group")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RuleSet:
        if self.source.role_id is not None and self.source.role_id != self.name:
            raise ValueError(
                f"source.role_id {self.source.role_id!r} must equal the rule set's name"
            )
        _no_duplicates([table.name for table in self.tables], "table name")
        self._check_row_rules()
        self._check_acyclic()
        return self

    def _check_row_rules(self) -> None:
        declared = {
            table.name: {*table.columns, *table.predicate_columns} for table in self.tables
        }

        def listed(name: str, where: str) -> None:
            if name not in declared:
                raise ValueError(f"{where}: table {name!r} is not listed in this rule set")

        def column(table: str, name: str, where: str) -> None:
            if name not in declared[table]:
                raise ValueError(f"{where}: column {name!r} is not declared on table {table!r}")

        for index, table in enumerate(self.tables):
            if table.row_rule is None:
                continue
            for node, path in _rule_nodes(table.row_rule, f"tables[{index}].row_rule"):
                if isinstance(node, CompareRule | IsNullRule | IsNotNullRule):
                    column(table.name, node.column, path)
                elif isinstance(node, OwnerViaRule):
                    column(table.name, node.id_column, path)
                    if node.link_table in declared:
                        column(node.link_table, node.link_row_column, path)
                        column(node.link_table, node.link_user_column, path)
                elif isinstance(node, InheritRule):
                    column(table.name, node.column, path)
                    listed(node.parent_table, path)
                    column(node.parent_table, node.parent_column, path)
                    if node.parent_rule is not None:
                        for local, local_path in _rule_nodes(
                            node.parent_rule, f"{path}.parent_rule"
                        ):
                            if not isinstance(local, _COLUMN_LOCAL):
                                raise ValueError(
                                    f"{local_path}: parent_rule cannot hold a "
                                    f"{local.kind!r} node, only {_COLUMN_LOCAL_KINDS}"
                                )
                            if not isinstance(local, AnyOfRule | AllOfRule):
                                column(node.parent_table, local.column, local_path)
                elif isinstance(node, ReferencedByRule):
                    column(table.name, node.id_column, path)
                    listed(node.source_table, path)
                    if node.link is None:
                        column(node.source_table, node.source_column or "", path)
                    else:
                        column(node.source_table, node.source_id_column or "", path)
                        if node.link.table in declared:
                            column(node.link.table, node.link.source_column, path)
                            column(node.link.table, node.link.target_column, path)

    def _check_acyclic(self) -> None:
        """Refuse a visibility-graph cycle: drop tables with no edge left until none drops."""
        listed = {table.name for table in self.tables}
        edges = {
            table.name: set(_rule_edges(table.row_rule, listed))
            for table in self.tables
            if table.row_rule is not None
        }
        remaining = dict(edges)
        removed = True
        while removed:
            removed = False
            for name, targets in list(remaining.items()):
                if not targets & remaining.keys():
                    del remaining[name]
                    removed = True
        if remaining:
            names = ", ".join(repr(name) for name in sorted(remaining))
            raise ValueError(f"row rules form a cycle through tables {names}")

    @property
    def confirmed(self) -> bool:
        return self.source.confirmed_against_production is True


class RuleSetFile(_Record):
    """The version-1 rule-set file."""

    format_version: StrictInt
    binding: Binding
    metadata: MetadataKeys
    rule_sets: list[RuleSet]

    @field_validator("format_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only format_version = 1 is supported")
        return value

    @field_validator("rule_sets")
    @classmethod
    def _unique_sets(cls, value: list[RuleSet]) -> list[RuleSet]:
        _no_duplicates([rule_set.name for rule_set in value], "rule set name")
        return value


class RuleSetFileError(ValueError):
    """The rule-set file is missing, unreadable, malformed or inconsistent with config."""


# --- row-rule compilation ------------------------------------------------------

_COMPARISONS: Mapping[str, type[exp.Binary]] = MappingProxyType({
    "=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE,
})

_USER_ID = "user_id"


def _slot_name(slot: int) -> str:
    return f"s{slot}"


def _slot_column(slot: int, column: str) -> exp.Column:
    return exp.column(
        exp.to_identifier(column, quoted=True),
        table=exp.to_identifier(_slot_name(slot), quoted=True),
    )


def _number(text: str) -> exp.Expression:
    # The parser reads a leading minus as Neg over a positive literal; the
    # re-walk compares trees, so the builder must produce that shape too.
    if text.startswith("-"):
        return exp.Neg(this=exp.Literal.number(text[1:]))
    return exp.Literal.number(text)


def float_literal_text(value: float) -> str:
    """``value`` as a 17-significant-digit exponent-form literal, e.g. ``5.0000000000000000e-01``.

    MySQL reads a literal in exponent form as a double, and 17 significant
    digits read back as the same double.
    """
    return f"{value:.16e}"


def _literal(value: Any, value_type: str) -> exp.Expression:
    if value_type == "boolean":
        return exp.Boolean(this=value)
    if value_type == "string":
        return exp.Literal.string(value)
    if value_type == "float":
        return _number(float_literal_text(value))
    return _number(str(value))


def _exists(table: str, slot: int, condition: exp.Expression) -> exp.Exists:
    source = exp.Table(
        this=exp.to_identifier(table, quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier(_slot_name(slot), quoted=True)),
    )
    body = (
        exp.Select(expressions=[exp.Literal.number(1)])
        .from_(source, copy=False)
        .where(condition, copy=False)
    )
    return exp.Exists(this=body)


@dataclass(frozen=True)
class CompiledRule:
    """A table's row rule as a sqlglot condition over alias slots and the user ID.

    The condition qualifies every column with a slot identifier (``s0`` is the
    scoped table itself, ``s1`` onwards each table read inside the rule) and
    holds the user ID as a placeholder; :meth:`instantiate` fills both per
    statement. ``tables`` names every table the filter reads.
    """

    condition: exp.Expression
    slots: int
    tables: frozenset[str]

    def instantiate(self, aliases: Sequence[str], user_id: int) -> exp.Expression:
        if len(aliases) != self.slots:
            raise ValueError(f"rule needs {self.slots} aliases, got {len(aliases)}")
        names = {_slot_name(slot): alias for slot, alias in enumerate(aliases)}
        tree = self.condition.copy()
        for node in list(tree.walk()):
            if isinstance(node, exp.Placeholder):
                node.replace(exp.Literal.number(user_id))
            elif isinstance(node, exp.Column):
                node.set("table", exp.to_identifier(names[node.table], quoted=True))
            elif isinstance(node, exp.TableAlias):
                node.set("this", exp.to_identifier(names[node.name], quoted=True))
        return tree


MAX_EXISTS_DEPTH = 16
"""The most ``EXISTS`` subqueries a compiled row rule may nest inside one another."""

MAX_SLOTS = 64
"""The most slots (aliased tables, the filtered table included) a compiled row rule may use."""


class _RuleLimitError(Exception):
    """A row rule passed :data:`MAX_EXISTS_DEPTH` or :data:`MAX_SLOTS` while compiling."""


class _Compiler:
    def __init__(self, rule_set: RuleSet) -> None:
        self._rules = {table.name: table.row_rule for table in rule_set.tables}
        self._slots = 0
        self._depth = 0
        self._tables: set[str] = set()

    def _new_slot(self, table: str) -> int:
        # Checked before allocating, so a rule whose full compile is huge
        # stops at the limit instead of compiling everything first.
        if self._slots >= MAX_SLOTS:
            raise _RuleLimitError(
                f"the compiled rule needs more than {MAX_SLOTS} slots, the limit"
            )
        self._tables.add(table)
        slot = self._slots
        self._slots += 1
        return slot

    @contextlib.contextmanager
    def _deeper(self) -> Iterator[None]:
        """One more ``EXISTS`` level around whatever is compiled inside."""
        if self._depth >= MAX_EXISTS_DEPTH:
            raise _RuleLimitError(
                f"the compiled rule nests more than {MAX_EXISTS_DEPTH} EXISTS "
                "subqueries inside one another, the limit"
            )
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def compile(self, table: str) -> CompiledRule:
        rule = self._rules[table]
        if rule is None:
            raise ValueError(f"table {table!r} has no row rule")
        self._slots = 0
        self._depth = 0
        self._tables = set()
        base = self._new_slot(table)
        condition = self._node(rule, base)
        return CompiledRule(condition=condition, slots=self._slots, tables=frozenset(self._tables))

    def _own_rule(self, table: str, slot: int, condition: exp.Expression) -> exp.Expression:
        rule = self._rules.get(table)
        return condition if rule is None else exp.and_(condition, self._node(rule, slot))

    def _node(self, node: RowRule, slot: int) -> exp.Expression:
        if isinstance(node, CompareRule):
            column = _slot_column(slot, node.column)
            if node.request is not None:
                return _COMPARISONS[node.operator](
                    this=column, expression=exp.Placeholder(this=_USER_ID)
                )
            if node.operator == "in":
                return exp.In(
                    this=column, expressions=[_literal(v, node.value_type) for v in node.value]
                )
            return _COMPARISONS[node.operator](
                this=column, expression=_literal(node.value, node.value_type)
            )
        if isinstance(node, IsNullRule):
            return exp.Is(this=_slot_column(slot, node.column), expression=exp.Null())
        if isinstance(node, IsNotNullRule):
            # Parenthesized: under HIGH_NOT_PRECEDENCE, MySQL reads a bare
            # ``NOT x IS NULL`` as ``(NOT x) IS NULL``.
            return exp.Not(
                this=exp.Paren(
                    this=exp.Is(this=_slot_column(slot, node.column), expression=exp.Null())
                )
            )
        if isinstance(node, AnyOfRule):
            return exp.or_(*(self._node(child, slot) for child in node.rules))
        if isinstance(node, AllOfRule):
            return exp.and_(*(self._node(child, slot) for child in node.rules))
        if isinstance(node, OwnerViaRule):
            with self._deeper():
                link = self._new_slot(node.link_table)
                return _exists(node.link_table, link, exp.and_(
                    exp.EQ(
                        this=_slot_column(link, node.link_row_column),
                        expression=_slot_column(slot, node.id_column),
                    ),
                    exp.EQ(
                        this=_slot_column(link, node.link_user_column),
                        expression=exp.Placeholder(this=_USER_ID),
                    ),
                ))
        if isinstance(node, InheritRule):
            with self._deeper():
                parent = self._new_slot(node.parent_table)
                match = exp.EQ(
                    this=_slot_column(parent, node.parent_column),
                    expression=_slot_column(slot, node.column),
                )
                condition = self._own_rule(node.parent_table, parent, match)
                if node.parent_rule is not None:
                    condition = exp.and_(condition, self._node(node.parent_rule, parent))
                return _exists(node.parent_table, parent, condition)
        if isinstance(node, ReferencedByRule):
            if node.link is None:
                with self._deeper():
                    source = self._new_slot(node.source_table)
                    match = exp.EQ(
                        this=_slot_column(source, node.source_column or ""),
                        expression=_slot_column(slot, node.id_column),
                    )
                    return _exists(
                        node.source_table,
                        source,
                        self._own_rule(node.source_table, source, match),
                    )
            with self._deeper():
                link = self._new_slot(node.link.table)
                # The source EXISTS sits inside the link EXISTS: two levels.
                with self._deeper():
                    source = self._new_slot(node.source_table)
                    source_match = exp.EQ(
                        this=_slot_column(source, node.source_id_column or ""),
                        expression=_slot_column(link, node.link.source_column),
                    )
                    source_exists = _exists(
                        node.source_table,
                        source,
                        self._own_rule(node.source_table, source, source_match),
                    )
                link_match = exp.EQ(
                    this=_slot_column(link, node.link.target_column),
                    expression=_slot_column(slot, node.id_column),
                )
                return _exists(
                    node.link.table,
                    link,
                    exp.and_(self._own_rule(node.link.table, link, link_match), source_exists),
                )
        raise TypeError(f"unknown row-rule node {type(node).__name__}")


def _listed_columns(rule_set: RuleSet) -> dict[str, tuple[str, ...]]:
    return {table.name: tuple(table.columns) for table in rule_set.tables}


@dataclass(frozen=True)
class GuardSpec:
    """What the rule-set guard enforces for one set.

    ``tables`` maps each listed table to its listed ``columns``; ``rules``
    maps each table with a row rule to its compiled rule.
    """

    tables: Mapping[str, tuple[str, ...]]
    rules: Mapping[str, CompiledRule]


@dataclass
class CatalogConfirmation:
    """Whether the catalog existence check has passed once in this process."""

    confirmed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class LoadedRuleSets:
    """A validated rule-set file, indexed for request selection and the SQL guard."""

    path: Path
    file: RuleSetFile
    # Lowercase hex SHA-256 of the bytes load_rule_sets parsed; "" when built without them.
    sha256: str = ""
    catalog: CatalogConfirmation = field(default_factory=CatalogConfirmation)
    compiled: dict[str, dict[str, CompiledRule]] = field(default_factory=dict)

    def rule_set(self, name: str) -> RuleSet | None:
        for rule_set in self.file.rule_sets:
            if rule_set.name == name:
                return rule_set
        return None

    def memory_groups_for(self, name: str) -> tuple[str, ...]:
        """The ``memory_groups`` of rule set ``name``; empty when it lists none or is absent."""
        rule_set = self.rule_set(name)
        if rule_set is None or rule_set.memory_groups is None:
            return ()
        return tuple(rule_set.memory_groups)

    @property
    def memory_groups(self) -> frozenset[str]:
        """Every group some rule set's ``memory_groups`` lists."""
        return frozenset(
            group for rule_set in self.file.rule_sets for group in rule_set.memory_groups or ()
        )

    def tables_for(self, name: str) -> dict[str, tuple[str, ...]]:
        """The listed tables of rule set ``name`` and each one's listed ``columns``."""
        rule_set = self.rule_set(name)
        if rule_set is None:
            raise KeyError(name)
        return _listed_columns(rule_set)

    def guard_spec(self, name: str) -> GuardSpec:
        """Rule set ``name``'s listed tables and compiled row rules; ``KeyError`` if absent.

        Raises ``RuntimeError`` when a table of the set has a row rule with no
        compiled rule, so the guard refuses rather than serving the table
        unfiltered.
        """
        tables = self.tables_for(name)
        rule_set = self.rule_set(name)
        compiled = self.compiled.get(name, {})
        if rule_set is not None and any(
            table.row_rule is not None and table.name not in compiled for table in rule_set.tables
        ):
            raise RuntimeError(f"rule set {name!r} has a row rule that was not compiled")
        return GuardSpec(tables=tables, rules=MappingProxyType(dict(compiled)))

    @property
    def unconfirmed(self) -> list[str]:
        return [rule_set.name for rule_set in self.file.rule_sets if not rule_set.confirmed]


def _record_path(loc: tuple[Any, ...]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or "(file root)"


def _validation_problems(exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors(include_url=False):
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        problems.append(f"{_record_path(tuple(err.get('loc', ())))}: {msg}")
    return problems


def _url_database(url: str) -> str:
    # Must name the database the MySQL runner connects to for the same URL;
    # build_sql_runner in agent/factory.py strips every leading slash.
    return (urlparse(url).path or "").lstrip("/")


def load_rule_sets(path: Path, *, database_url: str) -> LoadedRuleSets:
    """Read, parse and validate the rule-set file at ``path`` against ``database_url``.

    Raises ``RuleSetFileError`` for an unreadable file, a file that is not
    UTF-8 text, invalid TOML, TOML nested too deeply to parse (``RecursionError``),
    a format violation (each problem named by its record path), a ``database_url``
    that is not a MySQL URL, a ``binding.database`` that differs, byte for byte,
    from the database the URL's path names, a row rule that passes
    :data:`MAX_EXISTS_DEPTH` or :data:`MAX_SLOTS` while it is compiled, or a row
    rule that raises any other exception while it is compiled or proved (each
    named by its record path).
    """
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # Translate line endings exactly as read_text did before the hash was
        # added: tomllib rejects a bare CR, so parsing the raw text would stop a
        # bare-CR file that loads today from loading.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        data = tomllib.loads(text)
    except OSError as exc:
        raise RuleSetFileError(
            f"rule-set file {path} could not be read: {exc.strerror or type(exc).__name__}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise RuleSetFileError(f"rule-set file {path} is not UTF-8 text") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuleSetFileError(f"rule-set file {path} is not valid TOML: {exc}") from exc
    except RecursionError as exc:
        raise RuleSetFileError(
            f"rule-set file {path} nests its values too deeply to be parsed"
        ) from exc
    try:
        rule_set_file = RuleSetFile.model_validate(data)
    except ValidationError as exc:
        problems = "\n  ".join(_validation_problems(exc))
        raise RuleSetFileError(f"rule-set file {path} is invalid:\n  {problems}") from None
    scheme = database_url.split("://", 1)[0].lower() if "://" in database_url else ""
    if not (scheme == "mysql" or scheme.startswith("mysql+")):
        raise RuleSetFileError(
            "rule sets need a MySQL database; database.url is not a mysql:// or "
            "mysql+<driver>:// URL"
        )
    url_database = _url_database(database_url)
    if rule_set_file.binding.database != url_database:
        raise RuleSetFileError(
            f"rule-set file {path}: binding.database {rule_set_file.binding.database!r} "
            "is not the database named in database.url"
        )
    return LoadedRuleSets(
        path=path,
        file=rule_set_file,
        sha256=digest,
        compiled=_compile_all(path, rule_set_file),
    )


def _compile_all(path: Path, rule_set_file: RuleSetFile) -> dict[str, dict[str, CompiledRule]]:
    """Compile and prove every row rule; any exception fails the load naming the rule."""
    from sqllens.safety.rule_sets import prove_compiled_rule

    compiled: dict[str, dict[str, CompiledRule]] = {}
    for set_index, rule_set in enumerate(rule_set_file.rule_sets):
        compiler = _Compiler(rule_set)
        rules: dict[str, CompiledRule] = {}
        tables = _listed_columns(rule_set)
        for table_index, table in enumerate(rule_set.tables):
            if table.row_rule is None:
                continue
            where = f"rule_sets[{set_index}].tables[{table_index}].row_rule"
            try:
                rule = compiler.compile(table.name)
                prove_compiled_rule(table.name, tables[table.name], rule)
            except _RuleLimitError as exc:
                raise RuleSetFileError(
                    f"rule-set file {path} is invalid:\n  {where}: the row rule is over a "
                    f"load limit: {exc}"
                ) from exc
            except Exception as exc:
                raise RuleSetFileError(
                    f"rule-set file {path} is invalid:\n  {where}: the row rule could not "
                    f"be compiled ({type(exc).__name__}: {exc})"
                ) from exc
            rules[table.name] = rule
        compiled[rule_set.name] = rules
    return compiled


# --- request selection -----------------------------------------------------

REFUSED_RULE_SET_KEY_MISSING = "Request refused: rule-set key missing"
REFUSED_NO_SUCH_RULE_SET = "Request refused: no such rule set"
REFUSED_ACCOUNT_NOT_BOUND = "Request refused: account not bound"
REFUSED_USER_ID_INVALID = "Request refused: user ID invalid"
REFUSED_RULE_SET_NOT_CONFIRMED = "Request refused: rule set not confirmed"
REFUSED_CONVERSATION_OWNER = (
    "Request refused: this conversation was started under another rule set, account or user"
)
REFUSED_CATALOG_UNCONFIRMED = (
    "Request refused: the rule-set file could not be confirmed against the database"
)


class RuleSetRequestError(RuntimeError):
    """A rule-set request refused before its agent runs. The message names only the class.

    ``refusal_class`` is the class without the ``Request refused: `` prefix.
    ``rule_set``, ``account_id`` and ``user_id`` hold the values that passed
    their checks before the refusal, ``None`` otherwise; they feed the audit line.
    """

    def __init__(
        self,
        message: str,
        *,
        rule_set: str | None = None,
        account_id: str | None = None,
        user_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_class = message.removeprefix("Request refused: ")
        self.rule_set = rule_set
        self.account_id = account_id
        self.user_id = user_id


_KEPT_METADATA_KEYS: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "sqllens_rule_set_kept_metadata_keys", default=None
)


def set_kept_metadata_keys(
    keys: tuple[str, ...] | None,
) -> contextvars.Token[tuple[str, ...] | None]:
    """Publish the request's kept ``_meta`` key names; reset the token in ``finally``."""
    return _KEPT_METADATA_KEYS.set(keys)


def reset_kept_metadata_keys(token: contextvars.Token[tuple[str, ...] | None]) -> None:
    _KEPT_METADATA_KEYS.reset(token)


METADATA_DISCARDED_NOTE = "SQL Lens discarded the request _meta for exceeding its limits"


def refusal_log_fields(
    refusal_class: str,
    keys: Iterable[str] | None = None,
    *,
    metadata_discarded: bool = False,
) -> str:
    """A refusal's class and the names, never the values, of its kept ``_meta`` keys.

    ``keys`` defaults to the names published for the current request. Both
    are JSON-encoded with ASCII escapes, so a caller-chosen key name cannot
    break the log line. With ``metadata_discarded`` no key is named.
    """
    fields = f"class={json.dumps(refusal_class)}"
    if metadata_discarded:
        return f"{fields}; {METADATA_DISCARDED_NOTE}"
    if keys is None:
        keys = _KEPT_METADATA_KEYS.get() or ()
    return f"{fields}; kept _meta keys={json.dumps(sorted(str(key) for key in keys))}"


def audit_line(
    outcome: str,
    *,
    rule_set: str | None = None,
    account_id: str | None = None,
    user_id: int | None = None,
) -> str:
    """The one INFO audit line of a rule-set request; identity values left ``None`` are omitted."""
    record: dict[str, Any] = {"outcome": outcome}
    for name, value in (("rule_set", rule_set), ("account_id", account_id), ("user_id", user_id)):
        if value is not None:
            record[name] = value
    return "rule-set audit: " + json.dumps(record)


@dataclass(frozen=True)
class RuleSetSelection:
    """The identity a rule-set request runs under."""

    name: str
    account_id: str
    user_id: int

    @property
    def user_key(self) -> str:
        return f"rule-set:{self.name}/account:{self.account_id}/user:{self.user_id}"


_SELECTION_VAR: contextvars.ContextVar[RuleSetSelection | None] = contextvars.ContextVar(
    "sqllens_rule_set_selection", default=None
)


def set_rule_set_selection(
    selection: RuleSetSelection | None,
) -> contextvars.Token[RuleSetSelection | None]:
    """Publish the request's selection; the token must be reset in ``finally``."""
    return _SELECTION_VAR.set(selection)


def reset_rule_set_selection(token: contextvars.Token[RuleSetSelection | None]) -> None:
    _SELECTION_VAR.reset(token)


def current_rule_set_selection() -> RuleSetSelection | None:
    """The selection published for the current request, or ``None``."""
    return _SELECTION_VAR.get()


def _decimal_text(value: object) -> str | None:
    """A string as written, or a non-Boolean integer as decimal text; else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _user_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _ASCII_DIGITS_RE.fullmatch(value):
        try:
            number = int(value)
        except ValueError:
            # Past Python's int/str digit limit: no user ID is that long.
            return None
        return number if number > 0 else None
    return None


def select_rule_set(loaded: LoadedRuleSets, metadata: Mapping[str, Any]) -> RuleSetSelection:
    """Apply the five request classes in order; raise ``RuleSetRequestError`` at the first.

    The classes are: rule-set key missing or empty; rule-set name not a
    string or whole number, or naming no set; account key missing, or its
    decimal text not in ``binding.account_ids``; user key missing or not a
    positive whole number; the named set unconfirmed.
    """
    keys = loaded.file.metadata
    if keys.rule_set not in metadata or metadata[keys.rule_set] == "":
        raise RuleSetRequestError(REFUSED_RULE_SET_KEY_MISSING)
    name = _decimal_text(metadata[keys.rule_set])
    rule_set = loaded.rule_set(name) if name is not None else None
    if rule_set is None:
        raise RuleSetRequestError(REFUSED_NO_SUCH_RULE_SET)
    # An unconfirmed set has not passed its checks, so the audit line must not
    # name it: pass ``rule_set`` only once ``confirmed`` is known to hold.
    passed_set = rule_set.name if rule_set.confirmed else None
    account = _decimal_text(metadata.get(keys.account_id))
    if account is None or account not in loaded.file.binding.account_ids:
        raise RuleSetRequestError(REFUSED_ACCOUNT_NOT_BOUND, rule_set=passed_set)
    user_id = _user_id(metadata.get(keys.user_id)) if keys.user_id in metadata else None
    if user_id is None:
        raise RuleSetRequestError(
            REFUSED_USER_ID_INVALID, rule_set=passed_set, account_id=account
        )
    if not rule_set.confirmed:
        raise RuleSetRequestError(
            REFUSED_RULE_SET_NOT_CONFIRMED, account_id=account, user_id=user_id
        )
    return RuleSetSelection(name=rule_set.name, account_id=account, user_id=user_id)


# --- catalog existence check -------------------------------------------------


class RuleSetCatalogError(Exception):
    """A listed table or column is not in the connected database as listed."""


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


_Column = tuple[str, str]


def _join_pairs(table: str, node: RowRule) -> list[tuple[_Column, _Column]]:
    """The (table, column) pairs ``node`` compares with each other; none for a non-join node."""
    if isinstance(node, InheritRule):
        return [((table, node.column), (node.parent_table, node.parent_column))]
    if isinstance(node, OwnerViaRule):
        return [((table, node.id_column), (node.link_table, node.link_row_column))]
    if isinstance(node, ReferencedByRule):
        if node.link is None:
            return [((table, node.id_column), (node.source_table, node.source_column or ""))]
        return [
            ((table, node.id_column), (node.link.table, node.link.target_column)),
            ((node.source_table, node.source_id_column or ""),
             (node.link.table, node.link.source_column)),
        ]
    return []


def _catalog_reads(loaded: LoadedRuleSets) -> tuple[
    dict[str, list[str]], list[tuple[str, str, str]], list[tuple[_Column, _Column]]
]:
    """The tables and columns to check, in file order, each typed comparison and each join.

    The first value maps every listed table, then every unlisted ``owner_via``
    or ``referenced_by`` link table, to its columns; the second lists (table,
    column, value_type) for every ``compare`` column, ``parent_rule`` ones
    included, and every ``owner_via`` ``link_user_column`` (compared with the
    integer user ID); the third lists every join pair: an ``inherit``
    ``column`` and its ``parent_column``, a direct ``referenced_by``
    ``id_column`` and its ``source_column``, a linked ``referenced_by``
    ``id_column`` and ``link.target_column`` and its ``source_id_column`` and
    ``link.source_column``, and an ``owner_via`` ``id_column`` and its
    ``link_row_column``.
    """
    reads: dict[str, list[str]] = {}
    links: dict[str, list[str]] = {}
    typed: list[tuple[str, str, str]] = []
    joins: list[tuple[_Column, _Column]] = []
    for rule_set in loaded.file.rule_sets:
        listed = {table.name for table in rule_set.tables}
        for table in rule_set.tables:
            reads.setdefault(table.name, []).extend([*table.columns, *table.predicate_columns])
            if table.row_rule is None:
                continue
            for node, _ in _rule_nodes(table.row_rule, ""):
                joins.extend(_join_pairs(table.name, node))
                if isinstance(node, CompareRule):
                    typed.append((table.name, node.column, node.value_type))
                elif isinstance(node, InheritRule) and node.parent_rule is not None:
                    typed.extend(
                        (node.parent_table, local.column, local.value_type)
                        for local, _ in _rule_nodes(node.parent_rule, "")
                        if isinstance(local, CompareRule)
                    )
                elif isinstance(node, OwnerViaRule):
                    typed.append((node.link_table, node.link_user_column, "integer"))
                    if node.link_table not in listed:
                        links.setdefault(node.link_table, []).extend(
                            [node.link_row_column, node.link_user_column]
                        )
                elif (
                    isinstance(node, ReferencedByRule)
                    and node.link is not None
                    and node.link.table not in listed
                ):
                    links.setdefault(node.link.table, []).extend(
                        [node.link.source_column, node.link.target_column]
                    )
    for table, columns in links.items():
        reads.setdefault(table, []).extend(columns)
    return reads, typed, joins


def _join_family(data_type: str) -> str:
    """``data_type``'s join family: ``integer``, ``string``, or the type itself."""
    for family in ("integer", "string"):
        if data_type in VALUE_TYPE_DATA_TYPES[family]:
            return family
    return data_type


def check_catalog(connection: Any, loaded: LoadedRuleSets) -> None:
    """Confirm every table and column the file names exists in the connected database.

    Reads ``information_schema`` for the connection's current database
    (``DATABASE()``, the database ``database.url`` names). Every table any set
    lists, and every unlisted ``owner_via`` or ``referenced_by`` link table,
    must exist with exactly that name, byte for byte, as a ``BASE TABLE``;
    every listed column and predicate column, and each unlisted link table's
    two named columns, must exist on it with exactly that name. Every
    ``compare`` column's ``DATA_TYPE`` (a ``parent_rule``'s ``compare``
    columns read on its ``parent_table``) must be one :data:`VALUE_TYPE_DATA_TYPES` accepts for its
    ``value_type``, and every ``owner_via`` ``link_user_column``'s one it
    accepts for ``integer``. The two columns of every join pair (see
    :func:`_catalog_reads`) must both be in the integer family
    (``VALUE_TYPE_DATA_TYPES["integer"]``), both in the string family
    (``VALUE_TYPE_DATA_TYPES["string"]``), or, when either is outside both
    families, have identical ``DATA_TYPE`` values; a column with no
    ``DATA_TYPE``, or on a table that is not a base table, is left to the
    existence check. Raises ``RuleSetCatalogError`` naming each
    mismatch; a catalog read that fails raises the driver's exception.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        kinds = {_text(row[0]): _text(row[1]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        columns: dict[str, dict[str, str]] = {}
        for row in cursor.fetchall():
            columns.setdefault(_text(row[0]), {})[_text(row[1])] = _text(row[2]).lower()
    finally:
        cursor.close()

    reads, typed, joins = _catalog_reads(loaded)
    problems: list[str] = []
    for table, wanted in reads.items():
        kind = kinds.get(table)
        if kind is None:
            folded = [name for name in kinds if name.lower() == table.lower()]
            if folded:
                problems.append(
                    f"table {table!r} differs in letter case from the database's {folded[0]!r}"
                )
            else:
                problems.append(f"table {table!r} does not exist")
            continue
        if kind != "BASE TABLE":
            problems.append(f"table {table!r} is a {kind.lower()}, not a base table")
            continue
        present = columns.get(table, {})
        for column in dict.fromkeys(wanted):
            if column in present:
                continue
            folded = [name for name in present if name.lower() == column.lower()]
            if folded:
                problems.append(
                    f"column {table}.{column} differs in letter case from the "
                    f"database's {folded[0]!r}"
                )
            else:
                problems.append(f"column {table}.{column} does not exist")
    for table, column, value_type in dict.fromkeys(typed):
        data_type = columns.get(table, {}).get(column)
        if kinds.get(table) != "BASE TABLE" or data_type is None:
            continue
        if data_type not in VALUE_TYPE_DATA_TYPES[value_type]:
            problems.append(
                f"column {table}.{column} has type {data_type!r}, which a row rule's "
                f"value_type {value_type!r} does not accept"
            )
    for left, right in dict.fromkeys(joins):
        left_type = columns.get(left[0], {}).get(left[1])
        right_type = columns.get(right[0], {}).get(right[1])
        if (
            kinds.get(left[0]) != "BASE TABLE"
            or kinds.get(right[0]) != "BASE TABLE"
            or left_type is None
            or right_type is None
        ):
            continue
        if _join_family(left_type) != _join_family(right_type):
            problems.append(
                f"join columns {left[0]}.{left[1]} (type {left_type!r}) and "
                f"{right[0]}.{right[1]} (type {right_type!r}) have types a row rule "
                "cannot join"
            )
    if problems:
        raise RuleSetCatalogError(
            "the rule-set file does not match the database: " + "; ".join(problems)
        )


def mysql_connect_kwargs(url: str) -> dict[str, Any]:
    """PyMySQL ``connect`` arguments for ``url``, credentials percent-decoded."""
    from urllib.parse import unquote

    parsed = urlparse(url)
    if not parsed.hostname or not parsed.username:
        raise ValueError("mysql url must include user and host")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password) if parsed.password else "",
        "database": _url_database(url),
    }


_CATALOG_CONNECT_TIMEOUT_SECONDS = 5
# Bounds each socket read and write of the request-time check. The check runs
# under the catalog lock, so a stalled read would otherwise hold every later
# rule-set request; a timeout raises, and the caller refuses the request.
_CATALOG_IO_TIMEOUT_SECONDS = 30


def _check_catalog_blocking(url: str, loaded: LoadedRuleSets) -> None:
    import contextlib

    import pymysql

    connection = pymysql.connect(
        **mysql_connect_kwargs(url),
        connect_timeout=_CATALOG_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_CATALOG_IO_TIMEOUT_SECONDS,
        write_timeout=_CATALOG_IO_TIMEOUT_SECONDS,
    )
    with contextlib.closing(connection):
        check_catalog(connection, loaded)


async def confirm_catalog(url: str, loaded: LoadedRuleSets) -> None:
    """Run the existence check unless it already passed once, on its own connection.

    Raises ``RuleSetCatalogError`` naming each mismatch, or the exception that
    stopped the check; a pass marks the catalog confirmed. A failed check runs
    again on the next call.
    """
    state = loaded.catalog
    if state.confirmed:
        return
    async with state.lock:
        if state.confirmed:
            return
        from sqllens.agent.integrations.sql_threads import run_blocking_sql

        await run_blocking_sql(_check_catalog_blocking, url, loaded)
        state.confirmed = True


async def ensure_catalog_confirmed(url: str, loaded: LoadedRuleSets) -> None:
    """:func:`confirm_catalog` for a request; raise ``RuleSetRequestError`` on failure.

    The cause (a mismatched table or column, or the error that stopped the
    check) is logged at WARNING; the raised message names no table or column.
    """
    try:
        await confirm_catalog(url, loaded)
    except RuleSetCatalogError as exc:
        logger.warning("rule-set catalog check failed: %s", exc)
        raise RuleSetRequestError(REFUSED_CATALOG_UNCONFIRMED) from exc
    except Exception as exc:
        logger.warning(
            "rule-set catalog check could not run (%s); refusing the request",
            type(exc).__name__,
            exc_info=True,
        )
        raise RuleSetRequestError(REFUSED_CATALOG_UNCONFIRMED) from exc
