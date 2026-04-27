"""``list_data_sources`` MCP tool implementation."""

from __future__ import annotations

from sqllens.config import Config


def list_data_sources_impl(cfg: Config) -> str:
    """Return a Markdown summary of the configured database."""
    dialect = cfg.database.url.split("://", 1)[0] if "://" in cfg.database.url else "unknown"
    ro = "read-only" if cfg.database.read_only else "read-write"
    return (
        f"**Data Sources** (1 total)\n\n"
        f"- **{cfg.database.name}** ({dialect}, {ro})"
    )
