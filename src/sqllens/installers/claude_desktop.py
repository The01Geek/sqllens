# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""One-command installer that wires SQL Lens into Claude Desktop's MCP config.

Automates the runbook documented in
``docs/internal/installation/claude-desktop-windows-install.md``: writes a BOM-free
``sqllens.toml``, on Windows writes a ``.cmd`` launcher that bundles the
``command`` + ``args`` pair into the single-``command`` field Claude Desktop's
``mcpServers`` schema exposes, and merges a ``mcpServers`` entry into
``claude_desktop_config.json`` while preserving any existing ``preferences``
and sibling servers.

The module is intentionally a CLI-side concern, not under ``sqllens.tools``
which is reserved for MCP tool wrappers.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

PLATFORM_WIN = "win32"
PLATFORM_MAC = "darwin"
PLATFORM_LINUX_PREFIX = "linux"


def is_windows(platform_name: str) -> bool:
    return platform_name == PLATFORM_WIN


@dataclass(frozen=True)
class InstallOptions:
    """Fully-resolved options passed to :func:`run_install`.

    The CLI command calls :func:`resolve_options` to fill in OS-specific
    defaults from the user-supplied raw flags before constructing this.
    """

    db_url: str
    api_key: str
    name: str
    model: str
    read_only: bool
    memory_dir: str
    working_dir: Path
    config_path: Path


@dataclass(frozen=True)
class InstallResult:
    """Captures every decision the installer made, plus what actually changed."""

    options: InstallOptions
    platform_name: str
    server_command: str
    server_args: tuple[str, ...]
    toml_content: str
    cmd_path: Path | None
    cmd_content: str | None
    json_after: Mapping[str, Any]
    json_diff: str
    backup_path: Path | None
    toml_written: bool
    cmd_written: bool
    dry_run: bool
    preserved_sibling_servers: int
    args_prefix: tuple[str, ...] = ()

    @property
    def used_python_module_fallback(self) -> bool:
        return bool(self.args_prefix)

    @property
    def platform_label(self) -> str:
        if is_windows(self.platform_name):
            return "Windows"
        if self.platform_name == PLATFORM_MAC:
            return "macOS"
        if self.platform_name.startswith(PLATFORM_LINUX_PREFIX):
            return "Linux"
        return self.platform_name


@dataclass(frozen=True)
class _Invocation:
    """How the MCP client should spawn the SQL Lens server."""

    command: str
    args_prefix: tuple[str, ...] = ()

    @property
    def used_python_module_fallback(self) -> bool:
        return bool(self.args_prefix)


class InstallError(Exception):
    """Raised for any installer-level failure surfaced to the CLI."""


def default_working_dir(platform_name: str, env: Mapping[str, str]) -> Path:
    """Default writable directory for ``sqllens.toml`` and the launcher."""
    if is_windows(platform_name):
        user_profile = env.get("USERPROFILE")
        base = Path(user_profile) if user_profile else Path.home()
        return base / "sqllens"
    return Path.home() / ".sqllens"


def default_memory_dir(platform_name: str, env: Mapping[str, str]) -> Path:
    """Default ChromaDB persistence directory."""
    return default_working_dir(platform_name, env) / "chroma"


def default_config_path(platform_name: str, env: Mapping[str, str]) -> Path | None:
    """Detected ``claude_desktop_config.json`` location, or None on unknown platforms."""
    if is_windows(platform_name):
        appdata = env.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if platform_name == PLATFORM_MAC:
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if platform_name.startswith(PLATFORM_LINUX_PREFIX):
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    return None


def derive_default_name(db_url: str) -> str:
    """Pick a friendly entry name from the DSN.

    For sqlite, use the database file's stem (``./chinook.db`` -> ``chinook``).
    For other backends, use the database segment of the URL. Falls back to
    ``sqllens`` for anything unparseable.
    """
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        url = make_url(db_url)
        database = url.database or ""
    except (ArgumentError, ValueError):
        return "sqllens"
    if not database:
        return "sqllens"
    stem = Path(database).name or database
    for ext in (".db", ".sqlite", ".sqlite3"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem or "sqllens"


def resolve_invocation(
    *,
    platform_name: str,
    which: Callable[[str], str | None] = shutil.which,
    sys_executable: str = sys.executable,
) -> _Invocation:
    """Decide how the MCP client should launch SQL Lens.

    Prefers an absolute path to the ``sqllens`` script on PATH. Falls back to
    ``<python> -m sqllens`` when the script isn't installed in a globally
    discoverable location.
    """
    exe_name = "sqllens.exe" if is_windows(platform_name) else "sqllens"
    found = which(exe_name) or which("sqllens")
    if found:
        return _Invocation(command=found)
    return _Invocation(
        command=sys_executable,
        args_prefix=("-m", "sqllens"),
    )


def generate_toml(
    *,
    db_url: str,
    db_name: str,
    read_only: bool,
    model: str,
    memory_dir: str,
) -> str:
    """Render ``sqllens.toml`` as BOM-free UTF-8 text.

    The API key is intentionally omitted from the TOML — Claude Desktop will
    inject it via the ``env`` block in ``claude_desktop_config.json``.

    Uses TOML literal strings (single-quoted) for path fields so backslashes
    in Windows paths are taken verbatim, not interpreted as escapes.
    """
    return (
        "# SQL Lens configuration. Generated by `sqllens claude-desktop install`.\n"
        "# Edit by hand or re-run the installer to refresh.\n"
        "\n"
        "[database]\n"
        f"url = {_toml_string(db_url)}\n"
        f"name = {_toml_string(db_name)}\n"
        f"read_only = {'true' if read_only else 'false'}\n"
        "\n"
        "[llm]\n"
        'provider = "anthropic"\n'
        "# api_key is injected at runtime via SQLLENS_LLM__API_KEY\n"
        "# (set in the MCP client env block, not in this TOML).\n"
        f"model = {_toml_string(model)}\n"
        "\n"
        "[memory]\n"
        f"persist_dir = {_toml_string(memory_dir)}\n"
        'collection = "sqllens"\n'
        "similarity_threshold = 0.7\n"
        "\n"
        "[server]\n"
        'transport = "stdio"\n'
    )


def _toml_string(value: str) -> str:
    """Render *value* as a TOML string literal, picking the safe quoting style.

    Prefers single-quoted literal strings (no escape processing). If the value
    itself contains a single quote, falls back to a double-quoted basic string
    with the minimal set of escapes TOML requires.
    """
    if "'" not in value:
        return f"'{value}'"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def generate_cmd_launcher(
    *,
    working_dir: Path,
    server_command: str,
    server_args: list[str] | tuple[str, ...],
) -> str:
    r"""Render the Windows ``.cmd`` launcher body.

    Claude Desktop's ``mcpServers`` schema exposes a single ``command`` field
    (plus ``args``), and on Windows the most ergonomic way to pin both the
    binary and the working directory while staying within that schema is to
    point ``command`` at a tiny ``.cmd`` shim that ``cd``-s into the writable
    working directory and execs the server. The ``cd /d`` is belt-and-suspenders
    — it keeps the launcher useful for any future tooling that resolves paths
    against CWD, even though core agent scratch storage now lives under
    ``tempfile.gettempdir()``.
    """
    quoted_args = " ".join(_cmd_quote(arg) for arg in server_args)
    return (
        "@echo off\r\n"
        f"cd /d {_cmd_quote(str(working_dir))}\r\n"
        f"{_cmd_quote(server_command)} {quoted_args}\r\n"
    )


_CMD_QUOTE_TRIGGERS = (" ", "\t", "&", "(", ")", "%", "^", "<", ">", "|", "!", ";", ",")


def _cmd_quote(value: str) -> str:
    """Quote *value* for inclusion in a Windows ``.cmd`` line.

    Trigger set covers the cmd.exe metacharacters that change parsing outside
    quotes: ``& ( ) ^ < > |`` redirection/grouping, ``%`` and ``!`` expansion,
    plus ``; ,`` token splitting and whitespace. Inside quotes only ``%``,
    ``!`` (if delayed expansion is on), and ``"`` remain special — those are
    handled by the outer-quote wrap and the inner ``"`` doubling.
    """
    if not value:
        return '""'
    needs_quotes = any(ch in value for ch in _CMD_QUOTE_TRIGGERS)
    if not needs_quotes and '"' not in value:
        return value
    return '"' + value.replace('"', '""') + '"'


def build_mcp_entry(
    *,
    server_command: str,
    server_args: list[str],
    api_key: str,
) -> dict[str, Any]:
    """Build the ``mcpServers[<name>]`` value the installer merges in."""
    return {
        "command": server_command,
        "args": server_args,
        "env": {"SQLLENS_LLM__API_KEY": api_key},
    }


def merge_into_mcp_servers(
    existing: Mapping[str, Any] | None,
    *,
    name: str,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Return ``(new_dict, preserved_sibling_servers)``.

    Preserves every top-level key (e.g. ``preferences``) and every sibling
    server. The entry under *name* is overwritten in place — re-running the
    installer with the same name produces stable JSON.
    """
    if existing is None:
        new: dict[str, Any] = {}
    else:
        new = copy.deepcopy(dict(existing))
    servers = new.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(
            f"claude_desktop_config.json has a non-object 'mcpServers' value "
            f"(got {type(servers).__name__}); refusing to merge"
        )
    siblings = sum(1 for k in servers if k != name)
    servers[name] = entry
    return new, siblings


def make_backup_path(json_path: Path, now: datetime) -> Path:
    """Compute the timestamped ``.bak`` path the installer writes before mutation."""
    stamp = now.strftime("%Y%m%d%H%M%S")
    return json_path.with_name(json_path.name + f".bak.{stamp}")


def validate_toml(toml_path: Path, *, api_key: str) -> None:
    """Round-trip the generated TOML through :class:`sqllens.config.Config`.

    The API key is required at load time but is intentionally not stored in
    the TOML, so we set ``SQLLENS_LLM__API_KEY`` in the env for the duration
    of this check. ``Config.load`` also exports ``SQLLENS_CONFIG`` as a side
    effect, so both variables are snapshot-and-restored on exit (success or
    failure) to keep this function side-effect-free at the process-env level.
    """
    from sqllens.config import Config

    previous_key = os.environ.get("SQLLENS_LLM__API_KEY")
    previous_cfg = os.environ.get("SQLLENS_CONFIG")
    try:
        os.environ["SQLLENS_LLM__API_KEY"] = api_key
        Config.load(toml_path)
    finally:
        _restore_env("SQLLENS_LLM__API_KEY", previous_key)
        _restore_env("SQLLENS_CONFIG", previous_cfg)


def _restore_env(key: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def resolve_options(
    *,
    db_url: str,
    api_key: str | None,
    name: str | None,
    model: str,
    read_only: bool,
    memory_dir: Path | None,
    working_dir: Path | None,
    config_path: Path | None,
    platform_name: str,
    env: Mapping[str, str],
) -> InstallOptions:
    """Fill in OS-specific defaults and turn raw CLI args into ``InstallOptions``."""
    resolved_api_key = api_key or env.get("SQLLENS_LLM__API_KEY")
    if not resolved_api_key:
        raise InstallError(
            "An API key is required. Pass --api-key or set SQLLENS_LLM__API_KEY in your shell."
        )

    # Parse the DSN upfront so a typo fails before we write a TOML and a
    # ``.cmd`` launcher, then surface as a generic "validation failed" later.
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        make_url(db_url)
    except (ArgumentError, ValueError) as exc:
        raise InstallError(f"--db is not a valid SQLAlchemy URL: {exc}") from exc

    resolved_working = working_dir or default_working_dir(platform_name, env)
    resolved_memory = memory_dir or default_memory_dir(platform_name, env)
    resolved_name = name or derive_default_name(db_url)

    if config_path is None:
        detected = default_config_path(platform_name, env)
        if detected is None:
            raise InstallError(
                "Could not detect a Claude Desktop config path for this platform. "
                "Pass --config-path to override."
            )
        resolved_config = detected
    else:
        resolved_config = config_path

    return InstallOptions(
        db_url=db_url,
        api_key=resolved_api_key,
        name=resolved_name,
        model=model,
        read_only=read_only,
        memory_dir=str(resolved_memory),
        working_dir=resolved_working,
        config_path=resolved_config,
    )


def run_install(
    options: InstallOptions,
    *,
    dry_run: bool,
    force: bool,
    platform_name: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    now: Callable[[], datetime] | None = None,
) -> InstallResult:
    """Perform (or simulate) the install end-to-end.

    On dry-run, the function never touches disk and reports the plan. On a
    real run, TOML and ``.cmd`` failures revert their respective writes and
    leave Claude Desktop's JSON untouched; the JSON itself is updated
    atomically (tempfile + rename) so a kill mid-write cannot truncate the
    user's config. The working-directory ``mkdir`` is intentionally *not*
    reverted — it is idempotent and harmless on its own.
    """
    now_fn = now or (lambda: datetime.now(tz=UTC))

    invocation = resolve_invocation(platform_name=platform_name, which=which)

    toml_path = options.working_dir / "sqllens.toml"
    toml_content = generate_toml(
        db_url=options.db_url,
        db_name=options.name,
        read_only=options.read_only,
        model=options.model,
        memory_dir=options.memory_dir,
    )

    if is_windows(platform_name):
        embedded_args: tuple[str, ...] = (
            *invocation.args_prefix,
            "serve",
            "-c",
            str(toml_path),
        )
        cmd_path: Path | None = options.working_dir / "run-sqllens.cmd"
        cmd_content: str | None = generate_cmd_launcher(
            working_dir=options.working_dir,
            server_command=invocation.command,
            server_args=embedded_args,
        )
        json_command = str(cmd_path)
        json_args: tuple[str, ...] = ()
    else:
        cmd_path = None
        cmd_content = None
        json_command = invocation.command
        json_args = (*invocation.args_prefix, "serve", "-c", str(toml_path))

    entry = build_mcp_entry(
        server_command=json_command,
        server_args=list(json_args),
        api_key=options.api_key,
    )

    # During a dry-run, a brand-new machine without Claude Desktop installed
    # should still be able to preview the planned changes. Treat a missing
    # file as an empty config; on a real run, _read_existing_config still
    # raises so the user gets the "install Claude Desktop" hint before any
    # writes happen.
    if dry_run and not options.config_path.exists():
        json_before: dict[str, Any] = {}
    else:
        json_before = _read_existing_config(options.config_path)
    json_after, preserved_siblings = merge_into_mcp_servers(
        json_before, name=options.name, entry=entry
    )
    json_after_serialized = json.dumps(json_after, indent=2) + "\n"
    json_diff = _unified_json_diff(json_before, json_after, str(options.config_path))
    json_after_frozen: Mapping[str, Any] = MappingProxyType(json_after)

    if dry_run:
        return InstallResult(
            options=options,
            platform_name=platform_name,
            server_command=json_command,
            server_args=json_args,
            toml_content=toml_content,
            cmd_path=cmd_path,
            cmd_content=cmd_content,
            json_after=json_after_frozen,
            json_diff=json_diff,
            backup_path=None,
            toml_written=False,
            cmd_written=False,
            dry_run=True,
            preserved_sibling_servers=preserved_siblings,
            args_prefix=tuple(invocation.args_prefix),
        )

    try:
        options.working_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(
            f"Could not create working directory {options.working_dir}: {exc}"
        ) from exc

    existing_toml = _read_text_or_none(toml_path)
    toml_changed = existing_toml != toml_content
    if toml_changed and existing_toml is not None and not force:
        raise InstallError(
            f"{toml_path} already exists with different content. Pass --force to overwrite."
        )
    if toml_changed:
        try:
            toml_path.write_text(toml_content, encoding="utf-8")
        except OSError as exc:
            # A failed write may leave the file truncated; restore the prior
            # contents so the next run isn't blocked by a "different content"
            # gate that reflects our half-finished write, not the user's intent.
            _revert_toml(toml_path, existing_toml)
            raise InstallError(f"Could not write {toml_path}: {exc}") from exc

    cmd_written = False
    existing_cmd_bytes: bytes | None = None
    cmd_content_bytes: bytes | None = None
    if cmd_path is not None and cmd_content is not None:
        # Compare as bytes — ``Path.read_text`` collapses ``\r\n`` to ``\n``
        # under universal newlines, but ``generate_cmd_launcher`` deliberately
        # emits CRLF. A text-mode round-trip would re-trip the equality check
        # on every run and demand ``--force`` on each re-invocation.
        existing_cmd_bytes = _read_bytes_or_none(cmd_path)
        cmd_content_bytes = cmd_content.encode("utf-8")
        cmd_changed = existing_cmd_bytes != cmd_content_bytes
        if cmd_changed and existing_cmd_bytes is not None and not force:
            _revert_toml(toml_path, existing_toml)
            raise InstallError(
                f"{cmd_path} already exists with different content. Pass --force to overwrite."
            )
        if cmd_changed:
            try:
                cmd_path.write_bytes(cmd_content_bytes)
            except OSError as exc:
                # Revert both: the .cmd may be truncated after a partial write,
                # and the TOML write we just succeeded is meaningless without
                # the launcher Claude Desktop will exec.
                _revert_cmd_bytes(cmd_path, existing_cmd_bytes)
                _revert_toml(toml_path, existing_toml)
                raise InstallError(
                    f"Could not write {cmd_path}: {exc}"
                ) from exc
            cmd_written = True

    # pydantic's ValidationError and tomllib.TOMLDecodeError both subclass
    # ValueError; OSError covers filesystem hiccups in Config.load.
    # ImportError is included because Config.load can pull in optional
    # backends (e.g. SQLAlchemy psycopg) — a missing extra is a user-fixable
    # config issue, not an unexpected bug, and we still need to revert.
    try:
        validate_toml(toml_path, api_key=options.api_key)
    except (ValueError, OSError, ImportError) as exc:
        _revert_toml(toml_path, existing_toml)
        if cmd_path is not None and cmd_written:
            _revert_cmd_bytes(cmd_path, existing_cmd_bytes)
        raise InstallError(
            f"Generated sqllens.toml failed validation; aborting before touching "
            f"{options.config_path}.\n  Cause: {type(exc).__name__}: {exc}"
        ) from exc

    # Only mutate the user's JSON when the merge would actually change it.
    # Compare parsed dicts (not serialized bytes) so we don't re-normalize a
    # user's hand-formatted JSON (different indent, CRLF, no trailing newline)
    # on every run.
    backup_path: Path | None = None
    if json_after != json_before:
        try:
            backup_path = make_backup_path(options.config_path, now_fn())
            shutil.copy2(options.config_path, backup_path)
        except OSError as exc:
            raise InstallError(
                f"Failed to back up {options.config_path}: {exc}"
            ) from exc
        try:
            _atomic_write_text(options.config_path, json_after_serialized)
        except OSError as exc:
            # _atomic_write_text uses tempfile + os.replace; the original is
            # untouched unless os.replace itself failed mid-rename (rare:
            # Windows file-locking, EXDEV across mounts). Phrase the restore
            # hint so the user only clobbers their original if it's actually
            # missing or corrupt, not as a reflex on every write failure.
            hint = _restore_hint(platform_name, backup_path, options.config_path)
            raise InstallError(
                f"Failed to write {options.config_path}: {exc}. "
                f"A backup was made at {backup_path}; the original should still "
                f"be in place. If it is missing or corrupt, restore it with: {hint}"
            ) from exc

    return InstallResult(
        options=options,
        platform_name=platform_name,
        server_command=json_command,
        server_args=json_args,
        toml_content=toml_content,
        cmd_path=cmd_path,
        cmd_content=cmd_content,
        json_after=json_after_frozen,
        json_diff=json_diff,
        backup_path=backup_path,
        toml_written=toml_changed,
        cmd_written=cmd_written,
        dry_run=False,
        preserved_sibling_servers=preserved_siblings,
        args_prefix=tuple(invocation.args_prefix),
    )


def _read_existing_config(path: Path) -> dict[str, Any]:
    """Load Claude Desktop's config JSON, mapping a missing file to a clear error.

    A `FileNotFoundError` is translated into an actionable `InstallError` so
    the user gets "install Claude Desktop or pass --config-path" instead of
    a raw traceback. Other `OSError` (permission denied, I/O errors) is also
    surfaced through `InstallError` so the CLI's user-friendly channel handles
    it rather than the generic "file an issue" backstop.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InstallError(
            f"Claude Desktop config not found at {path}; "
            "install Claude Desktop or pass --config-path."
        ) from exc
    except OSError as exc:
        raise InstallError(f"Could not read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"{path} is not valid JSON; refusing to overwrite. Cause: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise InstallError(f"{path} does not contain a JSON object at the top level.")
    return loaded


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"Could not read {path}: {exc}") from exc


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"Could not read {path}: {exc}") from exc


def _revert_toml(toml_path: Path, original: str | None) -> None:
    """Best-effort restore of *toml_path*.

    The caller is in the middle of raising InstallError; we don't let an
    OSError in the revert shadow that. But silently swallowing leaves the
    user blind to a partially-failed revert (file may now be in a degraded
    state), and Typer's pretty error rendering won't surface the
    ``__cause__`` / ``__context__`` chain. Print a warning to stderr (with
    explicit flush so the process exit doesn't strand the line) so the user
    has at least one visible signal.
    """
    try:
        if original is None:
            toml_path.unlink(missing_ok=True)
        else:
            toml_path.write_text(original, encoding="utf-8")
    except OSError as exc:
        print(
            f"WARNING: failed to revert {toml_path}: {exc}. "
            "File may be in an inconsistent state.",
            file=sys.stderr,
            flush=True,
        )


def _revert_cmd_bytes(cmd_path: Path, original: bytes | None) -> None:
    """Bytes-mode counterpart of :func:`_revert_toml` for the Windows ``.cmd``."""
    try:
        if original is None:
            cmd_path.unlink(missing_ok=True)
        else:
            cmd_path.write_bytes(original)
    except OSError as exc:
        print(
            f"WARNING: failed to revert {cmd_path}: {exc}. "
            "File may be in an inconsistent state.",
            file=sys.stderr,
            flush=True,
        )


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (tempfile + os.replace).

    ``os.replace`` is atomic on POSIX and on Windows ≥ Vista, so a kill mid-
    write cannot leave the user's ``claude_desktop_config.json`` truncated.
    """
    encoded = content.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        # Cleanup is best-effort: a PermissionError on the unlink itself
        # would otherwise replace the original write failure, leaving the
        # user with a misleading message and a leaked tempfile either way.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _restore_hint(platform_name: str, backup_path: Path, config_path: Path) -> str:
    """Build a platform-appropriate copy command for restoring the backup."""
    if is_windows(platform_name):
        return f'copy "{backup_path}" "{config_path}"'
    return f"cp {backup_path} {config_path}"


def _unified_json_diff(before: dict[str, Any], after: dict[str, Any], label: str) -> str:
    before_lines = (json.dumps(before, indent=2) + "\n").splitlines(keepends=True)
    after_lines = (json.dumps(after, indent=2) + "\n").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{label} (before)",
            tofile=f"{label} (after)",
        )
    )


def format_install_result(result: InstallResult) -> list[str]:
    """Render *result* as a list of Rich-markup lines for the CLI to print.

    Lives here (not in cli.py) so the formatter has access to the same
    platform constants and dataclass internals it describes, and so the CLI
    layer stays a thin parse/dispatch shell.
    """
    opts = result.options
    lines: list[str] = [
        f"Detected platform: [bold]{result.platform_label}[/bold]",
        f"Claude Desktop config:  {opts.config_path}",
        f"Working directory:      {opts.working_dir}",
    ]

    if result.dry_run:
        lines.append("\n[bold]Dry run — nothing written.[/bold]")
        lines.append(f"\nWould write: {opts.working_dir / 'sqllens.toml'}")
        lines.append("\n[bold]sqllens.toml:[/bold]")
        lines.append(result.toml_content)
        if result.cmd_path is not None and result.cmd_content is not None:
            lines.append(f"\nWould write: {result.cmd_path}")
            lines.append("\n[bold]run-sqllens.cmd:[/bold]")
            lines.append(result.cmd_content)
        lines.append("\n[bold]claude_desktop_config.json diff:[/bold]")
        lines.append(result.json_diff or "(no change)")
        return lines

    toml_path = opts.working_dir / "sqllens.toml"
    if result.toml_written:
        lines.append(f"  - wrote {toml_path} (BOM-free UTF-8)")
    else:
        lines.append(f"  - sqllens.toml unchanged at {toml_path}")
    if result.cmd_path is not None:
        if result.cmd_written:
            lines.append(f"  - wrote {result.cmd_path} (Windows launcher shim)")
        else:
            lines.append(f"  - {result.cmd_path} unchanged")
    if result.used_python_module_fallback:
        lines.append("  - 'sqllens' was not found on PATH; using 'python -m sqllens' fallback")
    server_word = "server" if result.preserved_sibling_servers == 1 else "servers"
    if result.backup_path is None:
        lines.append(
            f"mcpServers['{opts.name}'] unchanged "
            f"({result.preserved_sibling_servers} sibling {server_word} preserved)."
        )
    else:
        lines.append(
            f"Merged '{opts.name}' into mcpServers "
            f"(preserved {result.preserved_sibling_servers} existing {server_word}, "
            "preferences untouched)."
        )
        lines.append(f"Backup written: {result.backup_path}")
    lines.append(
        "\n[yellow]Note:[/yellow] the API key is stored in plaintext in "
        f"{opts.config_path} (Claude Desktop's design)."
    )
    lines.append("\nDone. Restart Claude Desktop to pick up the new server.")
    return lines
