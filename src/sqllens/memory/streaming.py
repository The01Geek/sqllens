# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory-bounded streaming reader for the bundle JSON format.

Walks the nested ``sql_pairs.pairs`` and ``schema_docs`` arrays of a bundle
file and yields one record object at a time. Memory is bounded to roughly
one record (plus the read buffer) regardless of total file size, so a
100 MB bundle imports without loading the full document into RAM.

The hand-rolled scanner is string- and escape-aware: a ``}``, ``]``, or ``"``
inside a SQL string literal (e.g. ``WHERE name = '}'``) does not fool the
depth counter that frames each record.

This module is CLI-only (driven from the ``import-memory --stream`` path).
The MCP ``import_memory`` tool keeps the existing bounded parse + cap
guard in :mod:`sqllens.memory.io` to preserve the DoS contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import IO, Literal

from sqllens.memory.io import BundleFormatError

RecordKind = Literal["sql_pair", "schema_doc"]

# Default chunk size for buffered reads. Sized to amortize Python-level
# overhead per ``read()`` call while keeping the resident buffer well under
# any reasonable per-record cap. The buffer is compacted as it is consumed.
_DEFAULT_CHUNK = 64 * 1024

# Hard cap on a single value's byte length, in addition to per-item Pydantic
# validation in the caller. Defence-in-depth against a maliciously framed
# value whose contents would otherwise grow the buffer without bound (e.g.
# a single ``{`` followed by GBs of un-closed input). Per-item Pydantic caps
# (``QUESTION_MAX`` + ``SQL_MAX`` + ``CONTENT_MAX``) bound the legitimate
# upper limit at well under 100 KB even with serialization overhead; 1 MiB
# leaves generous headroom for whitespace / nesting without admitting an
# unbounded scan.
_MAX_VALUE_BYTES = 1 * 1024 * 1024


class _Scanner:
    """Forward-only character scanner over a text stream.

    Maintains an in-memory buffer that is refilled on demand and compacted
    when the consumed prefix gets large. The buffer is never expected to
    hold the entire file — only the current value being framed.
    """

    def __init__(self, fp: IO[str], chunk_size: int = _DEFAULT_CHUNK) -> None:
        self._fp = fp
        self._chunk_size = chunk_size
        self._buf = ""
        self._i = 0
        self._eof = False

    def _ensure(self, n: int) -> None:
        """Try to make ``_buf[_i:_i+n]`` available, reading from ``fp`` as needed."""
        while not self._eof and (len(self._buf) - self._i) < n:
            chunk = self._fp.read(self._chunk_size)
            if not chunk:
                self._eof = True
                break
            self._buf += chunk
        # Compact the consumed prefix when it grows large to keep memory bounded.
        if self._i > self._chunk_size * 4:
            self._buf = self._buf[self._i :]
            self._i = 0

    def peek(self) -> str:
        """Return the next character without consuming, or ``""`` at EOF."""
        self._ensure(1)
        if self._i >= len(self._buf):
            return ""
        return self._buf[self._i]

    def advance(self) -> str:
        """Consume and return one character. Raises at EOF."""
        c = self.peek()
        if not c:
            raise BundleFormatError("unexpected EOF while parsing bundle")
        self._i += 1
        return c

    def skip_whitespace(self) -> None:
        while True:
            c = self.peek()
            if not c or c not in " \t\n\r":
                return
            self._i += 1

    def expect(self, c: str) -> None:
        self.skip_whitespace()
        got = self.peek()
        if got != c:
            shown = repr(got) if got else "EOF"
            raise BundleFormatError(f"expected {c!r}, got {shown}")
        self._i += 1

    def read_value_raw(self) -> str:
        """Consume a complete JSON value and return its raw text.

        Frames ``{...}``, ``[...]``, strings, numbers, booleans, and ``null``
        by depth-tracked, string/escape-aware scanning. The returned text is
        a valid standalone JSON value; the caller can pass it to ``json.loads``.

        Bounded memory: a single value larger than ``_MAX_VALUE_BYTES`` raises
        ``BundleFormatError`` rather than growing the buffer without limit.
        """
        self.skip_whitespace()
        c = self.peek()
        if not c:
            raise BundleFormatError("unexpected EOF where a value was expected")

        start = self._i
        if c in "{[":
            self._scan_balanced(c)
        elif c == '"':
            self._scan_string()
        else:
            self._scan_primitive()

        raw = self._buf[start : self._i]
        # UTF-8 is at least one byte per code point, so a code-point check is
        # a sufficient (lower-bound) rejection — and it skips the ``encode``
        # of every returned value just to measure. ``_scan_balanced`` already
        # enforces the same cap in-loop; this catches strings / primitives.
        if len(raw) > _MAX_VALUE_BYTES:
            raise BundleFormatError(
                f"value exceeds the {_MAX_VALUE_BYTES}-byte streaming-record cap"
            )
        return raw

    def _scan_balanced(self, open_char: str) -> None:
        """Advance past a balanced ``{...}`` or ``[...]`` value.

        Tracks depth across both ``{}`` and ``[]`` since a SQL pair record
        may contain nested objects (``args`` etc.). Inside string literals
        (``"..."``) every character including ``}``/``]`` is treated as
        literal — that is the escape-awareness that prevents SQL like
        ``WHERE name = '}'`` from being mis-framed.
        """
        close_char = "}" if open_char == "{" else "]"
        depth = 0
        in_string = False
        escape_next = False
        i = self._i
        # Track size to avoid growing the buffer past the per-value cap.
        scan_start = i
        while True:
            # Refill in chunks rather than one char at a time.
            self._ensure(self._chunk_size)
            if i >= len(self._buf):
                raise BundleFormatError(
                    f"unexpected EOF inside {open_char}…{close_char}"
                )
            # Bound the byte length of an in-flight value. UTF-8 lower-bounds
            # at one byte per code point, so a code-point check is a cheap
            # over-approximation that protects against unbounded growth.
            if (i - scan_start) > _MAX_VALUE_BYTES:
                raise BundleFormatError(
                    f"value exceeds the {_MAX_VALUE_BYTES}-byte streaming-record cap"
                )
            ch = self._buf[i]
            if in_string:
                if escape_next:
                    escape_next = False
                elif ch == "\\":
                    escape_next = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
            i += 1
        self._i = i

    def _scan_string(self) -> None:
        """Advance past a JSON string literal at the current position."""
        i = self._i + 1  # skip opening quote
        escape_next = False
        while True:
            self._ensure(self._chunk_size)
            if i >= len(self._buf):
                raise BundleFormatError("unexpected EOF inside string literal")
            ch = self._buf[i]
            if escape_next:
                escape_next = False
            elif ch == "\\":
                escape_next = True
            elif ch == '"':
                i += 1
                break
            i += 1
        self._i = i

    def _scan_primitive(self) -> None:
        """Advance past a number / ``true`` / ``false`` / ``null``."""
        i = self._i
        while True:
            self._ensure(1)
            if i >= len(self._buf):
                break
            ch = self._buf[i]
            if ch in " \t\n\r,}]":
                break
            i += 1
        if i == self._i:
            raise BundleFormatError("expected a JSON value")
        self._i = i


def stream_records(fp: IO[str]) -> Iterator[tuple[RecordKind, dict]]:
    """Yield ``(kind, raw_obj)`` tuples for every record in a bundle file.

    ``kind`` is ``"sql_pair"`` (from ``sql_pairs.pairs``) or ``"schema_doc"``
    (from ``schema_docs``). ``raw_obj`` is the ``json.loads`` of each record
    fragment; the caller validates it against ``SqlPair`` / ``SchemaDoc`` so
    the per-item caps (``QUESTION_MAX``, ``SQL_MAX``, ``CONTENT_MAX``) fire
    row by row.

    Unknown top-level keys, ``training_type`` siblings, and any other
    structural noise in the document are skipped. Malformed JSON anywhere in
    the stream raises ``BundleFormatError``: the streaming reader cannot
    recover by skipping to the next comma without risking record loss, so
    the caller (CLI) surfaces the error and exits non-zero.
    """
    scanner = _Scanner(fp)
    scanner.skip_whitespace()
    if scanner.peek() == "":
        # An empty file is technically not a valid bundle, but ``{}`` is — the
        # caller (CLI) emits the "no records imported" warning when nothing
        # was yielded. Don't raise here: let the CLI decide what an empty
        # input means in context (it has the file size + dry-run knowledge).
        return
    scanner.expect("{")

    first = True
    while True:
        scanner.skip_whitespace()
        c = scanner.peek()
        if c == "}":
            scanner.advance()
            return
        if not first:
            if c != ",":
                shown = repr(c) if c else "EOF"
                raise BundleFormatError(
                    f"expected ',' or '}}' between bundle keys, got {shown}"
                )
            scanner.advance()
            scanner.skip_whitespace()
        first = False

        if scanner.peek() != '"':
            raise BundleFormatError("expected a string key at the bundle root")
        key_raw = scanner.read_value_raw()
        try:
            key = json.loads(key_raw)
        except json.JSONDecodeError as exc:
            raise BundleFormatError(f"invalid bundle key: {exc}") from exc
        if not isinstance(key, str):
            raise BundleFormatError("bundle root keys must be strings")
        scanner.expect(":")

        if key == "sql_pairs":
            yield from _stream_sql_pairs_block(scanner)
        elif key == "schema_docs":
            yield from _stream_array(scanner, "schema_doc")
        else:
            # Skip an unknown value (forward compatibility — the bundle
            # format may grow optional top-level keys).
            scanner.read_value_raw()


def _stream_sql_pairs_block(scanner: _Scanner) -> Iterator[tuple[RecordKind, dict]]:
    scanner.skip_whitespace()
    if scanner.peek() == "n":
        # JSON ``null`` — block is absent.
        scanner.read_value_raw()
        return
    scanner.expect("{")

    first = True
    while True:
        scanner.skip_whitespace()
        c = scanner.peek()
        if c == "}":
            scanner.advance()
            return
        if not first:
            if c != ",":
                shown = repr(c) if c else "EOF"
                raise BundleFormatError(
                    f"expected ',' or '}}' inside sql_pairs, got {shown}"
                )
            scanner.advance()
            scanner.skip_whitespace()
        first = False

        if scanner.peek() != '"':
            raise BundleFormatError("expected a string key inside sql_pairs")
        key_raw = scanner.read_value_raw()
        try:
            key = json.loads(key_raw)
        except json.JSONDecodeError as exc:
            raise BundleFormatError(f"invalid sql_pairs key: {exc}") from exc
        scanner.expect(":")
        if key == "pairs":
            yield from _stream_array(scanner, "sql_pair")
        else:
            # ``training_type`` and any future siblings: consume + discard.
            scanner.read_value_raw()


def _stream_array(
    scanner: _Scanner, kind: RecordKind
) -> Iterator[tuple[RecordKind, dict]]:
    scanner.skip_whitespace()
    if scanner.peek() == "n":
        scanner.read_value_raw()
        return
    scanner.expect("[")

    first = True
    while True:
        scanner.skip_whitespace()
        c = scanner.peek()
        if c == "]":
            scanner.advance()
            return
        if not first:
            if c != ",":
                shown = repr(c) if c else "EOF"
                raise BundleFormatError(
                    f"expected ',' or ']' inside {kind} array, got {shown}"
                )
            scanner.advance()
            scanner.skip_whitespace()
        first = False

        raw = scanner.read_value_raw()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BundleFormatError(
                f"invalid {kind} record: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise BundleFormatError(
                f"{kind} record must be a JSON object, got {type(obj).__name__}"
            )
        yield (kind, obj)
