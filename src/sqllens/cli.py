# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Command-line entrypoint for SQL Lens."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from sqllens import __version__

if TYPE_CHECKING:
    from sqllens.installers.claude_desktop import InstallResult

app = typer.Typer(
    name="sqllens",
    help="Natural-language SQL analytics over MCP.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
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
        console.print(f"[red]Invalid:[/red] {e}")
        raise typer.Exit(code=2) from e
    console.print("[green]Config OK[/green]")
    console.print(f"  database: {cfg.database.name} ({cfg.database.url.split('://')[0]})")
    console.print(f"  llm:      {cfg.llm.provider} / {cfg.llm.model}")
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

    _print_install_result(result)


def _print_install_result(result: InstallResult) -> None:
    opts = result.options
    if result.platform_name == "win32":
        platform_label = "Windows"
    elif result.platform_name == "darwin":
        platform_label = "macOS"
    elif result.platform_name.startswith("linux"):
        platform_label = "Linux"
    else:
        platform_label = result.platform_name
    console.print(f"Detected platform: [bold]{platform_label}[/bold]")
    console.print(f"Claude Desktop config:  {opts.config_path}")
    console.print(f"Working directory:      {opts.working_dir}")

    if result.dry_run:
        console.print("\n[bold]Dry run — nothing written.[/bold]")
        console.print(f"\nWould write: {opts.working_dir / 'sqllens.toml'}")
        console.print("\n[bold]sqllens.toml:[/bold]")
        console.print(result.toml_content)
        if result.cmd_path is not None and result.cmd_content is not None:
            console.print(f"\nWould write: {result.cmd_path}")
            console.print("\n[bold]run-sqllens.cmd:[/bold]")
            console.print(result.cmd_content)
        console.print("\n[bold]claude_desktop_config.json diff:[/bold]")
        console.print(result.json_diff or "(no change)")
        return

    if result.toml_written:
        console.print(f"  - wrote {opts.working_dir / 'sqllens.toml'} (BOM-free UTF-8)")
    else:
        console.print(f"  - sqllens.toml unchanged at {opts.working_dir / 'sqllens.toml'}")
    if result.cmd_path is not None:
        if result.cmd_written:
            console.print(f"  - wrote {result.cmd_path} (CWD launcher workaround for issue #10)")
        else:
            console.print(f"  - {result.cmd_path} unchanged")
    if result.used_python_module_fallback:
        console.print(
            "  - 'sqllens' was not found on PATH; using 'python -m sqllens' fallback"
        )
    server_word = "server" if result.preserved_sibling_servers == 1 else "servers"
    console.print(
        f"Merged '{opts.name}' into mcpServers "
        f"(preserved {result.preserved_sibling_servers} existing {server_word}, "
        "preferences untouched)."
    )
    if result.backup_path is not None:
        console.print(f"Backup written: {result.backup_path}")
    console.print(
        "\n[yellow]Note:[/yellow] the API key is stored in plaintext in "
        f"{opts.config_path} (Claude Desktop's design)."
    )
    console.print("\nDone. Restart Claude Desktop to pick up the new server.")


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
