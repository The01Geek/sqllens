# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Allow `python -m sqllens` to invoke the CLI."""

from sqllens.cli import app

if __name__ == "__main__":
    app()
