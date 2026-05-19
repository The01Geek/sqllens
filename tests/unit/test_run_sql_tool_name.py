# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Guard the run_sql tool-name contract.

Imported SQL pairs are stored with tool_name == RUN_SQL_TOOL_NAME so the agent
retrieves them at query time. If RunSqlTool's default name is ever renamed this
test fails loudly instead of silently breaking retrieval.
"""

from __future__ import annotations

from sqllens.agent.tools.run_sql import RunSqlTool
from sqllens.memory.store import RUN_SQL_TOOL_NAME


def test_run_sql_default_name_matches_constant() -> None:
    assert RunSqlTool(sql_runner=object()).name == RUN_SQL_TOOL_NAME
    assert RUN_SQL_TOOL_NAME == "run_sql"
