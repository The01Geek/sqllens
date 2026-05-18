# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Command-line entrypoint for SQL Lens."""

from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from sqllens import __version__
from sqllens.config import API_KEY_MISSING_MESSAGE

app = typer.Typer(
    name="sqllens",
    help="Natural-language SQL analytics over MCP.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

def _is_loopback_host(host: str) -> bool:
    # Recognizes the entire 127.0.0.0/8 IPv4 loopback range and ::1 (plus
    # IPv4-mapped IPv6 loopback like ::ffff:127.0.0.1), not just the canonical
    # spellings. No DNS resolution — wildcards ("0.0.0.0", "::") and arbitrary
    # external hostnames fail closed and must use bearer auth or the
    # SQLLENS_AUTH__INSECURE opt-out.
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_INSECURE_NON_LOOPBACK_MESSAGE = (
    "Refusing to start an unauthenticated HTTP server on a non-loopback interface "
    "(server.host={host!r}, auth.mode=none). Set SQLLENS_AUTH__MODE=bearer with a "
    "SQLLENS_AUTH__BEARER_TOKEN, or SQLLENS_AUTH__INSECURE=1 to override for "
    "closed-network deployments."
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
        console.print(f"[red]{path} already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=1)
    path.write_text(_SAMPLE_CONFIG)
    console.print(f"[green]Wrote {path}[/green]")
    console.print("Edit it, then run [bold]sqllens serve[/bold].")


@app.command()
def serve(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to sqllens.toml. Falls back to env / ./sqllens.toml."
    ),
) -> None:
    """Start the MCP server."""
    from sqllens.config import Config
    from sqllens.server import run

    try:
        cfg = Config.load(config)
    except Exception as e:
        console.print(f"[red]Config error:[/red] {escape(str(e))}")
        raise typer.Exit(code=2) from e
    if cfg.llm.api_key is None:
        console.print(f"[red]Config error:[/red] {escape(API_KEY_MISSING_MESSAGE)}")
        raise typer.Exit(code=2)
    if (
        cfg.server.transport == "http"
        and cfg.auth.mode == "none"
        and not _is_loopback_host(cfg.server.host)
    ):
        if not cfg.auth.insecure:
            console.print(
                f"[red]Refusing to start:[/red] "
                f"{escape(_INSECURE_NON_LOOPBACK_MESSAGE.format(host=cfg.server.host))}"
            )
            raise typer.Exit(code=2)
        console.print(
            f"[yellow]Warning:[/yellow] SQLLENS_AUTH__INSECURE=1 — starting "
            f"unauthenticated HTTP server on {escape(cfg.server.host)}. "
            "Closed-network deployments only."
        )
    run(cfg)


@app.command(name="validate")
def validate(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Validate config without starting the server."""
    from sqllens.config import Config

    try:
        cfg = Config.load(config)
    except Exception as e:
        console.print(f"[red]Invalid:[/red] {escape(str(e))}")
        raise typer.Exit(code=2) from e
    console.print("[green]Config OK[/green]")
    console.print(f"  database: {cfg.database.name} ({cfg.database.url.split('://')[0]})")
    llm_suffix = "" if cfg.llm.api_key is not None else " (api_key NOT SET)"
    console.print(f"  llm:      {cfg.llm.provider} / {cfg.llm.model}{llm_suffix}")
    console.print(f"  auth:     {cfg.auth.mode}")
    console.print(f"  transport: {cfg.server.transport}")


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
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # Friendly framing only; re-raising would dump a Python traceback right
        # after the framing line and contradict the "we've handled this" UX.
        # The chained exception is preserved on the typer.Exit via __cause__
        # for any future debug hook or test that inspects it.
        console.print(
            f"[red]Unexpected error:[/red] {type(exc).__name__}: {exc}\n"
            "This is likely a bug — please file an issue at "
            "https://github.com/The01Geek/sqllens/issues"
        )
        raise typer.Exit(code=2) from exc

    for line in format_install_result(result):
        console.print(line)


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

[auth]
mode = "none"            # one of: none, bearer, jwt

[server]
transport = "stdio"      # one of: stdio, http
host = "127.0.0.1"
port = 8765
"""


if __name__ == "__main__":
    app()
