# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Command-line entrypoint for SQL Lens."""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from sqllens import __version__
from sqllens.config import API_KEY_MISSING_MESSAGE

if TYPE_CHECKING:
    from sqllens.config import Config

app = typer.Typer(
    name="sqllens",
    help="Natural-language SQL analytics over MCP.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
# `sqllens serve` shares stdout with the stdio MCP JSON-RPC stream; routing
# errors to stderr keeps that channel clean. Other commands follow suit.
err_console = Console(stderr=True)


def _format_config_error(exc: Exception) -> str:
    """Render a config-load exception for stderr without leaking secrets.

    A pydantic ``ValidationError``'s ``str()`` can embed the offending input
    (bearer token, API key, DSN password) for schema-validation failures —
    including plain-``str`` fields like ``database.url`` whose value is not a
    self-masking ``SecretStr``. Emit only ``loc``/``msg``/``type``, dropping
    ``input``/``ctx``. Non-``ValidationError`` config-load errors (file-not-found,
    BOM, TOML syntax) are passed through: their messages are the actionable
    remediation and do not embed schema input. A ``tomllib`` *syntax* error on a
    secret-bearing line can still echo that line verbatim — config files are
    expected to be operator-readable, so that residual is accepted, not scrubbed.
    """
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines: list[str] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "")
        etype = err.get("type", "")
        prefix = f"{loc}: " if loc else ""
        lines.append(f"{prefix}{msg} [{etype}]")
    return "\n".join(lines)


def _is_loopback_host(host: str) -> bool:
    # Recognizes the entire 127.0.0.0/8 IPv4 loopback range and ::1, plus
    # IPv4-mapped IPv6 loopback (e.g. ::ffff:127.0.0.1) — the IPv4-mapped form
    # is unwrapped explicitly because CPython's IPv6Address.is_loopback returns
    # False for these on Python 3.11.x and 3.12.0-3.12.3 (gh-117566, fixed in
    # 3.12.4 / 3.13). No DNS resolution — wildcards ("0.0.0.0", "::") and
    # arbitrary external hostnames fail closed and must use bearer auth or
    # the SQLLENS_AUTH__INSECURE opt-out. The single literal hostname
    # "localhost" is matched case-insensitively (RFC 1035); no other
    # hostnames are recognized. Non-string input (None, ints, etc. from a
    # future refactor) also fails closed rather than raising — this is a
    # security guard and a traceback in place of a refusal would be misread
    # as "the guard didn't apply".
    try:
        if host.lower() == "localhost":
            return True
        addr = ipaddress.ip_address(host)
    except (ValueError, AttributeError, TypeError):
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


_INSECURE_NON_LOOPBACK_MESSAGE = (
    "Refusing to start an unauthenticated HTTP server on a non-loopback interface "
    "(server.host={host!r}, auth.mode=none). Set SQLLENS_AUTH__MODE=bearer with a "
    "SQLLENS_AUTH__BEARER_TOKEN, or SQLLENS_AUTH__INSECURE=1 to override for "
    "closed-network deployments."
)


def _loopback_policy_violated(cfg: Config) -> bool:
    """True when the unauthenticated-non-loopback policy condition holds.

    Callers (``serve``, ``validate``) combine this with ``cfg.auth.insecure``:
    when the policy condition holds and the operator has *not* set
    ``SQLLENS_AUTH__INSECURE=1`` they hard-fail; with the opt-out set they
    proceed and emit a visible breadcrumb. This helper does not consult
    ``cfg.auth.insecure`` itself so callers can phrase the warning/error
    message in surface-appropriate terms.
    """
    return (
        cfg.server.transport == "http"
        and cfg.auth.mode == "none"
        and not _is_loopback_host(cfg.server.host)
    )

def _version_callback(value: bool) -> None:
    if value:
        console.print(f"sqllens {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    pass


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"sqllens {__version__}")


@app.command()
def init(
    path: Path = typer.Option(
        Path("sqllens.toml"),
        "--path",
        help="Where to write the config file.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite if it exists."),
) -> None:
    """Write a sample sqllens.toml to the current directory."""
    if path.exists() and not force:
        err_console.print(f"[red]{path} already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=1)
    path.write_text(_SAMPLE_CONFIG)
    console.print(f"[green]Wrote {path}[/green]")
    console.print("Edit it, then run [bold]sqllens serve[/bold].")


@app.command()
def serve(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to sqllens.toml. Falls back to env / ./sqllens.toml."
    ),
    no_preflight: bool = typer.Option(
        False,
        "--no-preflight",
        envvar="SQLLENS_NO_PREFLIGHT",
        help=(
            "Skip eager DB/LLM/Chroma/auth probes. Useful in container "
            "orchestrators where dependencies come up after the server, or in "
            "tests. Otherwise leave on — the probes are your fail-fast guard."
        ),
    ),
) -> None:
    """Start the MCP server."""
    from sqllens.config import Config
    from sqllens.preflight import PreflightError, run_preflight
    from sqllens.server import run

    try:
        cfg = Config.load(config)
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {escape(_format_config_error(e))}")
        raise typer.Exit(code=2) from e
    if cfg.llm.api_key is None:
        err_console.print(f"[red]Config error:[/red] {escape(API_KEY_MISSING_MESSAGE)}")
        raise typer.Exit(code=2)
    if _loopback_policy_violated(cfg):
        if not cfg.auth.insecure:
            err_console.print(
                f"[red]Refusing to start:[/red] "
                f"{escape(_INSECURE_NON_LOOPBACK_MESSAGE.format(host=cfg.server.host))}"
            )
            raise typer.Exit(code=2)
        err_console.print(
            f"[yellow]Warning:[/yellow] SQLLENS_AUTH__INSECURE=1 — starting "
            f"unauthenticated HTTP server on {escape(cfg.server.host)}. "
            "Closed-network deployments only."
        )
    if no_preflight:
        err_console.print("[yellow]Preflight skipped (--no-preflight).[/yellow]")
    else:
        try:
            run_preflight(cfg)
        except PreflightError as e:
            err_console.print(f"[red]Preflight failed:[/red] {escape(str(e))}")
            raise typer.Exit(code=2) from e
    run(cfg)


@app.command(name="validate")
def validate(
    config: Path | None = typer.Option(None, "--config", "-c"),
    check_db: bool = typer.Option(False, "--check-db", help="Probe the database connection."),
    check_llm: bool = typer.Option(False, "--check-llm", help="Probe the LLM client."),
    check_memory: bool = typer.Option(
        False, "--check-memory", help="Probe the Chroma persist directory."
    ),
    check_auth: bool = typer.Option(False, "--check-auth", help="Probe the authenticator."),
) -> None:
    """Validate config without starting the server.

    Schema is always validated. Pass any combination of ``--check-*`` flags to
    also run the corresponding preflight probe — same checks ``serve`` runs.
    """
    from sqllens.config import Config
    from sqllens.preflight import (
        PreflightError,
        probe_auth,
        probe_database,
        probe_llm,
        probe_memory,
    )

    try:
        cfg = Config.load(config)
    except Exception as e:
        err_console.print(f"[red]Invalid:[/red] {escape(_format_config_error(e))}")
        raise typer.Exit(code=2) from e
    # Mirror serve's guard so CI / pre-deploy linting catches the
    # misconfiguration before `serve` would refuse to start.
    violated = _loopback_policy_violated(cfg)
    if violated and not cfg.auth.insecure:
        err_console.print(
            f"[red]Invalid:[/red] "
            f"{escape(_INSECURE_NON_LOOPBACK_MESSAGE.format(host=cfg.server.host))}"
        )
        raise typer.Exit(code=2)
    console.print("[green]Config OK[/green]")
    console.print(f"  database: {cfg.database.name} ({cfg.database.url.split('://')[0]})")
    llm_suffix = "" if cfg.llm.api_key is not None else " (api_key NOT SET)"
    console.print(f"  llm:      {cfg.llm.provider} / {cfg.llm.model}{llm_suffix}")
    auth_line = f"  auth:     {cfg.auth.mode}"
    if violated:
        auth_line += (
            f" [yellow](SQLLENS_AUTH__INSECURE=1 — non-loopback host "
            f"{escape(cfg.server.host)} with auth.mode=none)[/yellow]"
        )
    console.print(auth_line)
    console.print(f"  transport: {cfg.server.transport}")

    selected: list[tuple[str, Callable[..., None]]] = []
    if check_db:
        selected.append(("database", probe_database))
    if check_llm:
        selected.append(("llm", probe_llm))
    if check_memory:
        selected.append(("memory", probe_memory))
    if check_auth:
        selected.append(("auth", probe_auth))

    for label, probe in selected:
        try:
            probe(cfg)
        except PreflightError as e:
            err_console.print(f"[red]Preflight failed:[/red] {escape(str(e))}")
            raise typer.Exit(code=2) from e
        console.print(f"  [green]{label} OK[/green]")

    # Exit-code contract: 0 = OK, 1 = parses but would fail to start (api_key
    # unset), 2 = parse/schema error (raised above). Probes run before this so
    # --check-llm output is not suppressed by the early exit.
    if cfg.llm.api_key is None:
        err_console.print(
            f"[red]Would fail to start:[/red] {escape(API_KEY_MISSING_MESSAGE)}"
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# `sqllens claude-desktop ...` sub-app
# ---------------------------------------------------------------------------

claude_desktop_app = typer.Typer(
    name="claude-desktop",
    help="Wire SQL Lens into Claude Desktop's MCP configuration.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(claude_desktop_app, name="claude-desktop")


@claude_desktop_app.command(name="install")
def claude_desktop_install(
    db: str = typer.Option(
        ...,
        "--db",
        "-d",
        help="SQLAlchemy DSN. Same form accepted by [database].url.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "-k",
        help="Anthropic API key. Defaults to $SQLLENS_LLM__API_KEY if set.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Display name and mcpServers key. Derived from the DSN by default.",
    ),
    model: str = typer.Option(
        "claude-sonnet-4-5-20250929",
        "--model",
        help="Anthropic model id used by the agent.",
    ),
    memory_dir: Path | None = typer.Option(
        None,
        "--memory-dir",
        help="ChromaDB persistence directory. Defaults to <working-dir>/chroma.",
    ),
    working_dir: Path | None = typer.Option(
        None,
        "--working-dir",
        help="Where sqllens.toml (and the .cmd launcher on Windows) are written.",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config-path",
        help="Override the detected claude_desktop_config.json location.",
    ),
    read_only: bool = typer.Option(
        True,
        "--read-only/--no-read-only",
        help="Enforce read-only SQL (recommended).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the planned changes without writing anything.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing sqllens.toml / launcher with different content.",
    ),
) -> None:
    """Generate sqllens.toml, merge the server into Claude Desktop's MCP config."""
    from sqllens.installers.claude_desktop import (
        InstallError,
        format_install_result,
        resolve_options,
        run_install,
    )

    try:
        options = resolve_options(
            db_url=db,
            api_key=api_key,
            name=name,
            model=model,
            read_only=read_only,
            memory_dir=memory_dir,
            working_dir=working_dir,
            config_path=config_path,
            platform_name=sys.platform,
            env=os.environ,
        )
        result = run_install(options, dry_run=dry_run, force=force)
    except InstallError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # Friendly framing only; re-raising would dump a Python traceback right
        # after the framing line and contradict the "we've handled this" UX.
        # The chained exception is preserved on the typer.Exit via __cause__
        # for any future debug hook or test that inspects it.
        err_console.print(
            f"[red]Unexpected error:[/red] {type(exc).__name__}: {exc}\n"
            "This is likely a bug — please file an issue at "
            "https://github.com/The01Geek/sqllens/issues"
        )
        raise typer.Exit(code=2) from exc

    for line in format_install_result(result):
        console.print(line)


@app.command(name="import-memory")
def import_memory(
    path: Path = typer.Argument(..., help="Bundle file to import (JSON or CSV)."),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to sqllens.toml. Falls back to env / ./sqllens.toml."
    ),
    fmt: str = typer.Option("json", "--format", help="Bundle format (json or csv)."),
    clear: bool = typer.Option(
        False, "--clear", help="Wipe the collection before importing (prompts to confirm)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and report without writing anything."
    ),
    batch_size: int = typer.Option(
        100, "--batch-size", min=1, help="Writes issued before yielding."
    ),
) -> None:
    """Bulk-load a curated memory bundle into the configured store."""
    import asyncio

    from sqllens.config import Config
    from sqllens.memory import MemoryStore, import_bundle
    from sqllens.memory.io import BundleFormatError, parse_csv, parse_json

    if fmt not in ("json", "csv"):
        err_console.print(f"[red]Error:[/red] --format must be 'json' or 'csv' (got {escape(fmt)})")
        raise typer.Exit(code=1)
    try:
        cfg = Config.load(config)
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {escape(_format_config_error(e))}")
        raise typer.Exit(code=2) from e
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        err_console.print(f"[red]Error:[/red] cannot read {escape(str(path))}: {escape(str(e))}")
        raise typer.Exit(code=1) from e
    try:
        bundle = parse_csv(text) if fmt == "csv" else parse_json(text)
    except BundleFormatError as e:
        err_console.print(f"[red]Invalid bundle:[/red] {escape(str(e))}")
        raise typer.Exit(code=1) from e

    if clear and not dry_run:
        typer.confirm(
            f"This wipes every memory in collection '{cfg.memory.collection}'. Continue?",
            abort=True,
        )

    store = MemoryStore(cfg)
    report = asyncio.run(
        import_bundle(
            store, bundle, dry_run=dry_run, clear=clear, batch_size=batch_size
        )
    )

    prefix = "[yellow](dry-run)[/yellow] " if dry_run else ""
    console.print(
        f"{prefix}saved={report.saved} "
        f"skipped_duplicate={report.skipped_duplicate} "
        f"errors={len(report.errors)}"
    )
    for err in report.errors:
        err_console.print(
            f"  [red]{escape(err.kind)}[{err.index}]:[/red] {escape(err.message)}"
        )
    if report.errors:
        raise typer.Exit(code=1)


@app.command(name="export-memory")
def export_memory(
    path: Path = typer.Argument(..., help="Destination file for the exported bundle."),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to sqllens.toml. Falls back to env / ./sqllens.toml."
    ),
    fmt: str = typer.Option("json", "--format", help="Bundle format."),
) -> None:
    """Export the configured memory store to a bundle file."""
    from sqllens.config import Config
    from sqllens.memory import MemoryStore, export_bundle

    if fmt not in ("json", "csv"):
        err_console.print(f"[red]Error:[/red] --format must be 'json' or 'csv' (got {escape(fmt)})")
        raise typer.Exit(code=1)
    try:
        cfg = Config.load(config)
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {escape(_format_config_error(e))}")
        raise typer.Exit(code=2) from e

    store = MemoryStore(cfg)
    text = export_bundle(store, fmt)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        err_console.print(f"[red]Error:[/red] cannot write {escape(str(path))}: {escape(str(e))}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]Wrote {escape(str(path))}[/green]")


_SAMPLE_CONFIG = """\
# SQL Lens configuration. See https://github.com/The01Geek/sqllens for docs.

[database]
url = "sqlite:///./demo.db"
name = "primary"
read_only = true

[llm]
provider = "anthropic"
api_key = "sk-ant-..."   # or set SQLLENS_LLM__API_KEY env var
model = "claude-sonnet-4-5-20250929"

[memory]
persist_dir = "./chroma"
collection = "sqllens"
similarity_threshold = 0.7
# allow_import = false   # set true to expose the import_memory MCP tool
                         # (memory-poisoning risk — trusted operators only).
                         # The import-memory / export-memory CLI commands work
                         # regardless of this flag.

[auth]
mode = "none"            # one of: none, bearer (jwt is not implemented yet)
# For mode = "bearer", set a strong random token (>= 32 random bytes), e.g.
# generate one with:  openssl rand -hex 32
# bearer_token = "..."   # or set SQLLENS_AUTH__BEARER_TOKEN env var

[server]
transport = "stdio"      # one of: stdio, http
host = "127.0.0.1"
port = 8765
"""


if __name__ == "__main__":
    app()
