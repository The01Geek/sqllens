# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The connector suite fails, never skips, when its database settings are missing.

The connector jobs are merge checks; a skip there lets a job go green having run
no live-database test.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONNECTORS = ROOT / "tests" / "connectors"

_SKIP_CALLS = {"skip", "importorskip", "xfail"}
_SKIP_MARKS = {"skip", "skipif", "xfail"}


def _load_settings():
    spec = importlib.util.spec_from_file_location("connector_settings", CONNECTORS / "_settings.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["SQLLENS_TEST_MYSQL_URL", "SQLLENS_TEST_MYSQL_OTHER_DATABASE"])
@pytest.mark.parametrize("value", [None, ""])
def test_missing_setting_fails_naming_it(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    settings = _load_settings()
    assert name in (settings.MYSQL_URL, settings.MYSQL_OTHER_DATABASE)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(pytest.fail.Exception, match=name):
        settings.require_setting(name)


def test_present_setting_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_TEST_MYSQL_URL", "mysql+pymysql://u:p@h:3306/d")
    assert _load_settings().require_setting("SQLLENS_TEST_MYSQL_URL") == (
        "mysql+pymysql://u:p@h:3306/d"
    )


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def skip_forms(source: str) -> list[tuple[int, str]]:
    """Return ``(line, form)`` for each skip or expected-failure form in ``source``."""
    tree = ast.parse(source)
    pytest_names = {"pytest"}
    # Local name -> the pytest attribute path it stands for, e.g. {"mark": ["mark"]}.
    from_pytest: dict[str, list[str]] = {}
    skip_exceptions = {"SkipTest"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    pytest_names.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "pytest":
                    from_pytest[local] = [alias.name]
                elif node.module == "unittest" and alias.name == "SkipTest":
                    skip_exceptions.add(local)

    def pytest_path(node: ast.AST) -> list[str] | None:
        parts = _dotted(node)
        if not parts:
            return None
        if parts[0] in pytest_names:
            return parts[1:]
        if parts[0] in from_pytest:
            return from_pytest[parts[0]] + parts[1:]
        return None

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Name):
            path = pytest_path(node)
            if path == [] or path is None:
                continue
            if len(path) == 1 and path[0] in _SKIP_CALLS:
                found.append((node.lineno, f"pytest.{path[0]}"))
            elif len(path) == 2 and path[0] == "mark" and path[1] in _SKIP_MARKS:
                found.append((node.lineno, f"pytest.mark.{path[1]}"))
        elif isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            parts = _dotted(target)
            if parts and (
                parts == ["unittest", "SkipTest"]
                or (len(parts) == 1 and parts[0] in skip_exceptions)
            ):
                found.append((node.lineno, "raise SkipTest"))
    return sorted(set(found))


def _violations(directory: Path) -> list[str]:
    return [
        f"{path.relative_to(directory)}:{line}: {form}"
        for path in sorted(directory.glob("*.py"))
        for line, form in skip_forms(path.read_text())
    ]


def test_connector_suite_has_no_skip_or_xfail() -> None:
    assert sorted(CONNECTORS.glob("*.py")), "no connector test files found"
    assert _violations(CONNECTORS) == []


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\npytest.skip('x')\n",
        "import pytest\npytest.importorskip('pymysql')\n",
        "import pytest\npytest.xfail('x')\n",
        "import pytest\n@pytest.mark.skip\ndef test_a(): pass\n",
        "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a(): pass\n",
        "import pytest\n@pytest.mark.xfail\ndef test_a(): pass\n",
        "import pytest\npytestmark = pytest.mark.skip\n",
        "import pytest as pt\npt.skip('x')\n",
        "from pytest import skip\nskip('x')\n",
        "from pytest import importorskip as need\nneed('x')\n",
        "from pytest import mark\n@mark.xfail\ndef test_a(): pass\n",
        "import unittest\nraise unittest.SkipTest('x')\n",
        "from unittest import SkipTest\nraise SkipTest('x')\n",
        "from unittest import SkipTest\nraise SkipTest\n",
    ],
)
def test_source_check_flags_each_form(tmp_path: Path, source: str) -> None:
    (tmp_path / "test_planted.py").write_text(source)
    violations = _violations(tmp_path)
    assert violations and all(v.startswith("test_planted.py:") for v in violations)


def test_source_check_ignores_skip_named_keyword(tmp_path: Path) -> None:
    (tmp_path / "test_clean.py").write_text(
        "import pytest\n"
        "def build(**kw): return kw\n"
        "def test_a():\n"
        "    build(skip_catalog_views=True)\n"
        "    pytest.fail('x')\n"
    )
    assert _violations(tmp_path) == []
