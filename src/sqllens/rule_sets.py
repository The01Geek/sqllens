# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Per-role rule sets: the version-1 rule-set file, request selection and catalog check.

A rule-set file names, for one MySQL database, which tables and columns each
application role may read. ``Config`` loads and validates the file named by
``[rule_sets] file`` (see :func:`load_rule_sets`); ``query_database`` selects
one set per request from three ``_meta`` keys (:func:`select_rule_set`); and
:func:`check_catalog` confirms that every listed table and column exists in
the connected database. The SQL guard that enforces a selected set lives in
:mod:`sqllens.safety.rule_sets`.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import logging
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
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

VERIFY_MEMORY_UNSUPPORTED_MESSAGE = (
    "sqllens verify-memory does not support rule sets yet; run it against a config "
    "without [rule_sets] file"
)

ROW_RULE_MESSAGE = (
    "row rules are not supported yet; remove the whole rule set from the file, "
    "never only its row rules"
)


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
    """Provenance of a rule set. Validated, never sent to the model."""

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


class TableRule(_Record):
    """One table a rule set lets a request read, and its readable columns."""

    name: Identifier
    columns: list[Identifier] = Field(min_length=1)
    predicate_columns: list[Identifier] = Field(default_factory=list)
    row_rule: Any = None

    @model_validator(mode="after")
    def _columns(self) -> TableRule:
        if "row_rule" in self.model_fields_set:
            raise ValueError(ROW_RULE_MESSAGE)
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

    @model_validator(mode="after")
    def _consistent(self) -> RuleSet:
        if self.source.role_id is not None and self.source.role_id != self.name:
            raise ValueError(
                f"source.role_id {self.source.role_id!r} must equal the rule set's name"
            )
        _no_duplicates([table.name for table in self.tables], "table name")
        return self

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
    catalog: CatalogConfirmation = field(default_factory=CatalogConfirmation)

    def rule_set(self, name: str) -> RuleSet | None:
        for rule_set in self.file.rule_sets:
            if rule_set.name == name:
                return rule_set
        return None

    def tables_for(self, name: str) -> dict[str, tuple[str, ...]]:
        """The listed tables of rule set ``name`` and each one's listed ``columns``."""
        rule_set = self.rule_set(name)
        if rule_set is None:
            raise KeyError(name)
        return {table.name: tuple(table.columns) for table in rule_set.tables}

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
    UTF-8 text, invalid TOML, a format violation (each problem named by its
    record path), a ``row_rule``, a ``database_url`` that is not a MySQL URL, or a
    ``binding.database`` that differs, byte for byte, from the database the
    URL's path names.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuleSetFileError(
            f"rule-set file {path} could not be read: {exc.strerror or type(exc).__name__}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise RuleSetFileError(f"rule-set file {path} is not UTF-8 text") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuleSetFileError(f"rule-set file {path} is not valid TOML: {exc}") from exc
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
    return LoadedRuleSets(path=path, file=rule_set_file)


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
    """A rule-set request refused before its agent runs. The message names only the class."""


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
    account = _decimal_text(metadata.get(keys.account_id))
    if account is None or account not in loaded.file.binding.account_ids:
        raise RuleSetRequestError(REFUSED_ACCOUNT_NOT_BOUND)
    user_id = _user_id(metadata.get(keys.user_id)) if keys.user_id in metadata else None
    if user_id is None:
        raise RuleSetRequestError(REFUSED_USER_ID_INVALID)
    if not rule_set.confirmed:
        raise RuleSetRequestError(REFUSED_RULE_SET_NOT_CONFIRMED)
    return RuleSetSelection(name=rule_set.name, account_id=account, user_id=user_id)


# --- catalog existence check -------------------------------------------------


class RuleSetCatalogError(Exception):
    """A listed table or column is not in the connected database as listed."""


def check_catalog(connection: Any, loaded: LoadedRuleSets) -> None:
    """Confirm every listed table and column exists in the connected database.

    Reads ``information_schema`` for the connection's current database
    (``DATABASE()``, the database ``database.url`` names). Every table any set
    lists must exist with exactly the listed name, byte for byte, as a
    ``BASE TABLE``; every listed column and predicate column must exist on it
    with exactly the listed name. Raises ``RuleSetCatalogError`` naming each
    mismatch; a catalog read that fails raises the driver's exception.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        kinds = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        columns: dict[str, set[str]] = {}
        for row in cursor.fetchall():
            columns.setdefault(str(row[0]), set()).add(str(row[1]))
    finally:
        cursor.close()

    problems: list[str] = []
    # A table may be listed by several sets with different columns, so each
    # (table, column) pair is checked, and each table's own problem once.
    checked_tables: set[str] = set()
    checked_columns: set[tuple[str, str]] = set()
    for rule_set in loaded.file.rule_sets:
        for table in rule_set.tables:
            first_listing = table.name not in checked_tables
            checked_tables.add(table.name)
            kind = kinds.get(table.name)
            if kind is None:
                if not first_listing:
                    continue
                folded = [name for name in kinds if name.lower() == table.name.lower()]
                if folded:
                    problems.append(
                        f"table {table.name!r} differs in letter case from the database's "
                        f"{folded[0]!r}"
                    )
                else:
                    problems.append(f"table {table.name!r} does not exist")
                continue
            if kind != "BASE TABLE":
                if first_listing:
                    problems.append(
                        f"table {table.name!r} is a {kind.lower()}, not a base table"
                    )
                continue
            present = columns.get(table.name, set())
            for column in [*table.columns, *table.predicate_columns]:
                if column in present or (table.name, column) in checked_columns:
                    continue
                checked_columns.add((table.name, column))
                folded = [name for name in present if name.lower() == column.lower()]
                if folded:
                    problems.append(
                        f"column {table.name}.{column} differs in letter case from the "
                        f"database's {folded[0]!r}"
                    )
                else:
                    problems.append(f"column {table.name}.{column} does not exist")
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


async def ensure_catalog_confirmed(url: str, loaded: LoadedRuleSets) -> None:
    """Run the existence check unless it already passed once; raise ``RuleSetRequestError``.

    The cause (a mismatched table or column, or the error that stopped the
    check) is logged at WARNING; the raised message names no table or column.
    A failed check runs again on the next call.
    """
    state = loaded.catalog
    if state.confirmed:
        return
    async with state.lock:
        if state.confirmed:
            return
        from sqllens.agent.integrations.sql_threads import run_blocking_sql

        try:
            await run_blocking_sql(_check_catalog_blocking, url, loaded)
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
        state.confirmed = True
