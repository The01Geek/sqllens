# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Keep the SQL scratch capability usable without shell execution."""

from sqllens.agent import capabilities
from sqllens.agent.capabilities import file_system
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user import User
from sqllens.agent.integrations.local.file_system import LocalFileSystem


def test_file_system_does_not_expose_shell_execution(tmp_path) -> None:
    assert not hasattr(LocalFileSystem(str(tmp_path)), "run_bash")
    assert "run_bash" not in file_system.FileSystem.__abstractmethods__


def test_shell_result_is_not_part_of_capability_api() -> None:
    assert not hasattr(capabilities, "CommandResult")
    assert not hasattr(file_system, "CommandResult")
    assert not hasattr(file_system.models, "CommandResult")


async def test_sql_scratch_file_operations_remain_usable(tmp_path) -> None:
    fs = LocalFileSystem(str(tmp_path))
    context = ToolContext.model_construct(user=User(id="test-user"))
    contents = "value\n1\n"

    await fs.write_file("query_results_test.csv", contents, context)

    assert await fs.read_file("query_results_test.csv", context) == contents
    assert await fs.exists("query_results_test.csv", context)
    assert await fs.is_directory(".", context)
    assert await fs.list_files(".", context) == ["query_results_test.csv"]
    matches = await fs.search_files("value", context, include_content=True)
    assert [match.path for match in matches] == ["query_results_test.csv"]
