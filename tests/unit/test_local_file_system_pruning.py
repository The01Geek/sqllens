# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression guards for the pruned ``LocalFileSystem``.

Issue #218 pruned the upstream ``run_bash`` method (an
``asyncio.create_subprocess_shell`` sink) from the vendored ``LocalFileSystem``
and from the abstract ``FileSystem`` capability — the method had no in-repo
consumer, but it lived on the same FS instance the registered ``RunSqlTool``
already holds, so a future tool reusing that FS object would have inherited an
LLM-driven RCE seam. These tests pin the absence so a re-lift cannot silently
re-introduce it.
"""

from __future__ import annotations

import inspect

from sqllens.agent.capabilities.file_system import FileSystem
from sqllens.agent.integrations.local.file_system import LocalFileSystem


def test_local_file_system_has_no_subprocess_sink() -> None:
    """No attribute / method on ``LocalFileSystem`` shells out via subprocess.

    The check is broader than ``hasattr(..., "run_bash")`` on purpose: a future
    re-lift that renames the shell-execution method would otherwise bypass a
    name-only test. We scan every public attribute and refuse anything
    whose source references the ``subprocess`` module or ``create_subprocess``.
    """
    fs = LocalFileSystem("/tmp/sqllens-test")

    assert not hasattr(fs, "run_bash"), (
        "run_bash was pruned in #218 — a re-lift must drop it again"
    )

    for name in dir(fs):
        if name.startswith("_"):
            continue
        attr = getattr(fs, name)
        if not callable(attr):
            continue
        try:
            source = inspect.getsource(attr)
        except (OSError, TypeError):
            continue
        assert "subprocess" not in source, (
            f"LocalFileSystem.{name} appears to shell out (references "
            "'subprocess'); the #218 pruning forbids any subprocess sink on "
            "this class"
        )
        assert "create_subprocess" not in source, (
            f"LocalFileSystem.{name} appears to shell out (references "
            "'create_subprocess'); the #218 pruning forbids any subprocess "
            "sink on this class"
        )


def test_file_system_abc_has_no_subprocess_method() -> None:
    """The abstract base must not advertise a subprocess-shaped method.

    A future concrete implementer reading the ABC would otherwise feel obliged
    to implement ``run_bash`` (or a renamed equivalent) to satisfy
    ``@abstractmethod``, defeating the #218 pruning.
    """
    abstract_methods = FileSystem.__abstractmethods__
    assert "run_bash" not in abstract_methods, (
        "run_bash was pruned from the FileSystem ABC in #218 — re-adding it "
        "forces every concrete FS to ship a subprocess sink again"
    )
    # Defensive: refuse any new abstract method whose name hints at shell-out.
    for name in abstract_methods:
        lowered = name.lower()
        assert "bash" not in lowered and "shell" not in lowered and "exec" not in lowered, (
            f"FileSystem ABC declares abstract method {name!r} — names like "
            "bash/shell/exec are the #218 pruning's red flag"
        )
