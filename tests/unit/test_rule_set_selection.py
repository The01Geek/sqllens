# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Choosing a request's rule set from ``_meta``: the five request classes (issue #374)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sqllens.rule_sets import (
    REFUSED_ACCOUNT_NOT_BOUND,
    REFUSED_NO_SUCH_RULE_SET,
    REFUSED_RULE_SET_KEY_MISSING,
    REFUSED_RULE_SET_NOT_CONFIRMED,
    REFUSED_USER_ID_INVALID,
    LoadedRuleSets,
    RuleSetRequestError,
    RuleSetSelection,
    load_rule_sets,
    select_rule_set,
)

from ._rule_set_fixtures import MYSQL_URL, write_rule_set_file

VALID = {"role": "2", "account": "5", "user": 7}


@pytest.fixture
def loaded(tmp_path: Path) -> LoadedRuleSets:
    return load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)


def _with(**changes: Any) -> dict[str, Any]:
    meta = dict(VALID)
    for key, value in changes.items():
        if value is _MISSING:
            meta.pop(key, None)
        else:
            meta[key] = value
    return meta


_MISSING = object()


def test_valid_request_selects_the_set(loaded: LoadedRuleSets) -> None:
    assert select_rule_set(loaded, VALID) == RuleSetSelection("2", "5", 7)


@pytest.mark.parametrize(
    ("meta", "message"),
    [
        ({}, REFUSED_RULE_SET_KEY_MISSING),
        (_with(role=_MISSING), REFUSED_RULE_SET_KEY_MISSING),
        (_with(role=""), REFUSED_RULE_SET_KEY_MISSING),
        (_with(role="9"), REFUSED_NO_SUCH_RULE_SET),
        (_with(account=_MISSING), REFUSED_ACCOUNT_NOT_BOUND),
        (_with(account="7"), REFUSED_ACCOUNT_NOT_BOUND),
        (_with(account=""), REFUSED_ACCOUNT_NOT_BOUND),
        (_with(user=_MISSING), REFUSED_USER_ID_INVALID),
        (_with(role="3"), REFUSED_RULE_SET_NOT_CONFIRMED),
    ],
)
def test_each_request_class_is_refused(
    loaded: LoadedRuleSets, meta: dict[str, Any], message: str
) -> None:
    with pytest.raises(RuleSetRequestError) as info:
        select_rule_set(loaded, meta)
    assert str(info.value) == message


def test_classes_apply_in_order(loaded: LoadedRuleSets) -> None:
    """A request failing several classes is refused as the first that matches."""
    meta = {"role": "9", "account": "nope", "user": 0}
    with pytest.raises(RuleSetRequestError, match="no such rule set"):
        select_rule_set(loaded, meta)
    with pytest.raises(RuleSetRequestError, match="account not bound"):
        select_rule_set(loaded, {"role": "3", "account": "nope", "user": 0})
    with pytest.raises(RuleSetRequestError, match="user ID invalid"):
        select_rule_set(loaded, {"role": "3", "account": "5", "user": 0})


@pytest.mark.parametrize("name", [2, "2"])
def test_set_name_matches_as_decimal_text(loaded: LoadedRuleSets, name: object) -> None:
    assert select_rule_set(loaded, _with(role=name)).name == "2"


@pytest.mark.parametrize("name", ["02", 2.0, True, None, [2], {}])
def test_other_set_names_name_no_set(loaded: LoadedRuleSets, name: object) -> None:
    with pytest.raises(RuleSetRequestError) as info:
        select_rule_set(loaded, _with(role=name))
    assert str(info.value) == REFUSED_NO_SUCH_RULE_SET


@pytest.mark.parametrize("account", [5, "5"])
def test_account_matches_as_decimal_text(loaded: LoadedRuleSets, account: object) -> None:
    assert select_rule_set(loaded, _with(account=account)).account_id == "5"


@pytest.mark.parametrize("account", ["05", 5.0, True, None, [5]])
def test_other_accounts_are_not_bound(loaded: LoadedRuleSets, account: object) -> None:
    with pytest.raises(RuleSetRequestError, match="account not bound"):
        select_rule_set(loaded, _with(account=account))


@pytest.mark.parametrize("user", [7, "7", "007"])
def test_user_id_normalizes_to_integer(loaded: LoadedRuleSets, user: object) -> None:
    assert select_rule_set(loaded, _with(user=user)).user_id == 7


@pytest.mark.parametrize("user", [0, -1, "7a", True, 7.0, "", "0", "-1", "\u0667", None, [7]])
def test_invalid_user_ids_are_refused(loaded: LoadedRuleSets, user: object) -> None:
    with pytest.raises(RuleSetRequestError) as info:
        select_rule_set(loaded, _with(user=user))
    assert str(info.value) == REFUSED_USER_ID_INVALID


def test_refusal_text_never_echoes_meta_values(loaded: LoadedRuleSets) -> None:
    secretish = "account-8a7f3c"
    for meta in (
        {"role": secretish, "account": "5", "user": 7},
        {"role": "2", "account": secretish, "user": 7},
        {"role": "2", "account": "5", "user": secretish},
    ):
        with pytest.raises(RuleSetRequestError) as info:
            select_rule_set(loaded, meta)
        assert secretish not in str(info.value)


def test_empty_rule_sets_refuse_every_named_set(tmp_path: Path) -> None:
    from ._rule_set_fixtures import example_document

    document = example_document()
    document["rule_sets"] = []
    loaded = load_rule_sets(write_rule_set_file(tmp_path, document), database_url=MYSQL_URL)
    with pytest.raises(RuleSetRequestError, match="no such rule set"):
        select_rule_set(loaded, VALID)


def test_identity_key_distinguishes_set_account_and_user() -> None:
    keys = {
        RuleSetSelection("2", "5", 7).user_key,
        RuleSetSelection("3", "5", 7).user_key,
        RuleSetSelection("2", "6", 7).user_key,
        RuleSetSelection("2", "5", 8).user_key,
    }
    assert len(keys) == 4
