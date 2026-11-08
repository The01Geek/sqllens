# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Secret-column masking for query results.

``ResultMaskingRunner`` replaces every value in a result column whose label
matches the secret-word rule with :data:`WITHHELD_PLACEHOLDER`, before the
DataFrame reaches ``RunSqlTool`` and every sink built from it. The rule is a
heuristic over the column label alone; it never parses SQL text.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

import pandas as pd

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext


logger = logging.getLogger(__name__)


class ResultMaskingError(Exception):
    """Raised when the masking guard itself fails; the result is withheld."""


WITHHELD_PLACEHOLDER = "[withheld]"
MASKED_COLUMNS_ATTR = "masked_columns"

BUILTIN_SECRET_ENTRIES: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "passcode",
    "secret",
    "token",
    "hash",
    "salt",
    "pin",
    "otp",
    "totp",
    "mfa",
    "credential",
    "apikey",
    "api key",
    "access key",
    "private key",
    "encryption key",
    "signing key",
    "auth key",
    "recovery key",
    "master key",
    "session key",
    "one time code",
    "verification code",
    "reset code",
    "recovery code",
    "security code",
    "auth code",
    "sms code",
    "backup code",
)

# Built-in entries an operator may remove with ``unmasked_words``. They are the
# entries that commonly name non-secret data (``token_count``, ``commit_hash``,
# ``pin_code``). Every other built-in entry stays in force, so no setting turns
# masking off.
REMOVABLE_BUILTIN_ENTRIES: tuple[str, ...] = (
    "token",
    "hash",
    "salt",
    "pin",
    "otp",
    "totp",
    "mfa",
)


def _char_class(ch: str) -> str:
    if ch.isupper():
        return "U"
    if ch.isalpha():
        return "L"
    return "D"


def _split_chunk(chunk: str) -> list[str]:
    runs: list[tuple[str, str]] = []
    for ch in chunk:
        cls = _char_class(ch)
        if runs and runs[-1][0] == cls:
            runs[-1] = (cls, runs[-1][1] + ch)
        else:
            runs.append((cls, ch))
    words: list[str] = []
    i = 0
    while i < len(runs):
        cls, text = runs[i]
        nxt = runs[i + 1] if i + 1 < len(runs) else None
        if cls == "U" and nxt is not None and nxt[0] == "L":
            after = runs[i + 2] if i + 2 < len(runs) else None
            if nxt[1] == "s" and (after is None or after[0] == "D"):
                words.append(text + "s")
            else:
                if len(text) > 1:
                    words.append(text[:-1])
                words.append(text[-1] + nxt[1])
            i += 2
            continue
        words.append(text)
        i += 1
    return words


def split_label_words(label: str) -> list[str]:
    """Split a column label (or a configured entry) into lowercased words."""
    words: list[str] = []
    chunk = ""
    for ch in label:
        if ch.isalnum():
            chunk += ch
            continue
        if chunk:
            words.extend(_split_chunk(chunk))
        chunk = ""
    if chunk:
        words.extend(_split_chunk(chunk))
    return [w.lower() for w in words]


def entry_words(entry: str) -> tuple[str, ...]:
    """Split a configured entry or label, rejecting one that holds no letters or digits."""
    words = split_label_words(entry)
    if not words:
        raise ValueError(f"entry {entry!r} contains no letters or digits")
    return tuple(words)


def unmasked_column_label(entry: str) -> str:
    """Check an ``unmasked_columns`` entry, rejecting one that can never equal a label.

    A label is compared whole, so an entry with no letters or digits, or with
    leading or trailing whitespace, would load and silently exempt nothing.
    """
    entry_words(entry)
    if entry != entry.strip():
        raise ValueError(f"entry {entry!r} has leading or trailing whitespace")
    return entry


def removable_entry_words(entry: str) -> tuple[str, ...]:
    """Split an ``unmasked_words`` entry, rejecting one that is not a removable built-in."""
    words = entry_words(entry)
    if " ".join(words) not in REMOVABLE_BUILTIN_ENTRIES:
        raise ValueError(
            f"entry {entry!r} is not a removable built-in entry; "
            f"removable entries are: {', '.join(REMOVABLE_BUILTIN_ENTRIES)}"
        )
    return words


def _word_matches(word: str, entry_word: str) -> bool:
    return word in (entry_word, entry_word + "s", entry_word + "es")


def _label_matches(words: Sequence[str], entry: Sequence[str]) -> bool:
    span = len(entry)
    return any(
        all(_word_matches(words[start + k], entry[k]) for k in range(span))
        for start in range(len(words) - span + 1)
    )


class SecretColumnMatcher:
    """Decides which result column labels hold secrets."""

    def __init__(
        self,
        *,
        extra_words: Iterable[str] = (),
        unmasked_columns: Iterable[str] = (),
        unmasked_words: Iterable[str] = (),
    ) -> None:
        removed = {removable_entry_words(entry) for entry in unmasked_words}
        builtin = [entry_words(entry) for entry in BUILTIN_SECRET_ENTRIES]
        self._entries = [words for words in builtin if words not in removed]
        self._entries.extend(entry_words(entry) for entry in extra_words)
        self._unmasked = {unmasked_column_label(c).casefold() for c in unmasked_columns}

    def is_secret(self, label: object) -> bool:
        text = str(label)
        if text.casefold() in self._unmasked:
            return False
        words = split_label_words(text)
        return any(_label_matches(words, entry) for entry in self._entries)


def mask_secret_columns(df: pd.DataFrame, matcher: SecretColumnMatcher) -> pd.DataFrame:
    """Return ``df`` with every secret column's values replaced by the placeholder.

    The masked labels are recorded once each, in column order, under
    ``attrs[MASKED_COLUMNS_ATTR]`` of the returned frame. ``df`` itself is never
    modified. A masked-columns record already on ``df`` is not carried over, so
    the record names only the columns this call masked. A frame with no rows or
    no secret column comes back with its values unchanged: with no rows there
    is no value to withhold, so the model gets no withheld note.
    """
    positions = (
        [i for i, label in enumerate(df.columns) if matcher.is_secret(label)] if len(df) else []
    )
    attrs = {k: v for k, v in df.attrs.items() if k != MASKED_COLUMNS_ATTR}
    if not positions:
        if len(attrs) == len(df.attrs):
            return df
        out = df.copy(deep=False)
        out.attrs = attrs
        return out
    masked = df.copy()
    for i in positions:
        masked.isetitem(
            i, pd.Series([WITHHELD_PLACEHOLDER] * len(df), index=df.index, dtype=object)
        )
    masked.attrs = attrs
    masked.attrs[MASKED_COLUMNS_ATTR] = list(dict.fromkeys(str(df.columns[i]) for i in positions))
    return masked


class ResultMaskingRunner(SqlRunner):
    """Decorator that masks secret columns in the returned DataFrame."""

    def __init__(
        self,
        inner: SqlRunner,
        *,
        extra_words: Iterable[str] = (),
        unmasked_columns: Iterable[str] = (),
        unmasked_words: Iterable[str] = (),
    ) -> None:
        self._inner = inner
        self._matcher = SecretColumnMatcher(
            extra_words=extra_words,
            unmasked_columns=unmasked_columns,
            unmasked_words=unmasked_words,
        )

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        options = {"require_base_tables": True} if require_base_tables else {}
        df = await self._inner.run_sql(args, context, **options)
        try:
            return mask_secret_columns(df, self._matcher)
        except Exception as e:
            # Fail closed: an unexpected error in the masking step withholds
            # the whole result rather than returning the unmasked frame. The
            # exception's message can quote a cell value, so neither the log
            # nor the error the model reads carries it: log the type and the
            # stack frames so a guard bug stays diagnosable, and drop the
            # chained cause.
            logger.warning(
                "result masking guard raised an unexpected %s; failing closed\n%s",
                type(e).__name__,
                "".join(traceback.format_tb(e.__traceback__)),
            )
            raise ResultMaskingError(
                "refusing to return query result: result masking guard errored "
                f"({type(e).__name__})"
            ) from None
