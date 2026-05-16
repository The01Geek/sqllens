# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Command-line entrypoint for SQL Lens."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from sqllens import __version__

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
    if cfg.llm.api_key is None:
        console.print(
            "[red]Missing LLM API key.[/red] Set [bold]SQLLENS_LLM__API_KEY[/bold] "
            "in your environment, or add [bold]api_key[/bold] under "
            "[bold]\\[llm][/bold] in sqllens.toml."
        )
        raise typer.Exit(code=2)
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
