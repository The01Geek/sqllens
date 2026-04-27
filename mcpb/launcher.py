"""MCPB entry script.

Claude Desktop launches this file as the MCP server. It prepends the bundled
``vendor/`` directory to ``sys.path`` (so the host's Python doesn't need any
of our dependencies installed system-wide) and then hands off to the regular
SQL Lens stdio server.

The manifest's ``server.mcp_config.env`` already sets ``PYTHONPATH`` and the
``SQLLENS_*`` overrides, so by the time we get here the config is fully wired.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Belt-and-braces: the manifest sets PYTHONPATH already, but in case the host
# strips env vars we re-add the vendor directory at runtime.
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "vendor"
if _VENDOR.is_dir():
    vendor_str = str(_VENDOR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)


def main() -> None:
    """Run sqllens with stdio transport, regardless of TOML."""
    os.environ.setdefault("SQLLENS_SERVER__TRANSPORT", "stdio")
    # Defer import until after sys.path is fixed.
    from sqllens.cli import app

    # Force the ``serve`` subcommand. The MCPB user_config has no flag for
    # subcommand selection — they always want the server running.
    sys.argv = ["sqllens", "serve"]
    app()


if __name__ == "__main__":
    main()
