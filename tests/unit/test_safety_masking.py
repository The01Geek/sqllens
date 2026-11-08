# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Secret-column masking: word rule, guard, run_sql sinks, config and wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from sqllens.agent import User
from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import build_agent
from sqllens.agent.integrations.local import LocalFileSystem
from sqllens.agent.tools.run_sql import RunSqlTool
from sqllens.cli import app
from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ResultMaskingConfig,
    RlsRule,
)
from sqllens.safety import (
    MASKED_COLUMNS_ATTR,
    ReadOnlyGuardRunner,
    ResultMaskingRunner,
    RlsGuardRunner,
    RowCapRunner,
    _relation_check_executor,
    mark_truncation,
)
from sqllens.safety.masking import (
    BUILTIN_SECRET_ENTRIES,
    REMOVABLE_BUILTIN_ENTRIES,
    ResultMaskingError,
    SecretColumnMatcher,
    mask_secret_columns,
    split_label_words,
)
from sqllens.tools._format import components_to_blocks

from ._agent_stubs import StubAgentMemory

W = "[withheld]"

# --- word rule -------------------------------------------------------------


def test_builtin_entry_list_is_exactly_the_specified_31() -> None:
    assert BUILTIN_SECRET_ENTRIES == (
        "password", "passwd", "pwd", "passcode", "secret", "token", "hash", "salt",
        "pin", "otp", "totp", "mfa", "credential", "apikey", "api key", "access key",
        "private key", "encryption key", "signing key", "auth key", "recovery key",
        "master key", "session key", "one time code", "verification code",
        "reset code", "recovery code", "security code", "auth code", "sms code",
        "backup code",
    )
    assert len(BUILTIN_SECRET_ENTRIES) == 31


@pytest.mark.parametrize(
    ("label", "words"),
    [
        ("users.password_hash", ["users", "password", "hash"]),
        ("APIKey", ["api", "key"]),
        ("OTPSecret", ["otp", "secret"]),
        ("password2", ["password", "2"]),
        ("PINs", ["pins"]),
        ("PINs_x", ["pins", "x"]),
        ("PINs2", ["pins", "2"]),
        # An uppercase run plus "s" followed by a capital splits before the
        # run's last capital, as the issue's rule states; see NOT_MASKED.
        ("OTPsKey", ["ot", "ps", "key"]),
        ("PasswordHash", ["password", "hash"]),
        ("COLUMN_KEY", ["column", "key"]),
        ("", []),
        ("--", []),
    ],
)
def test_split_label_words(label: str, words: list[str]) -> None:
    assert split_label_words(label) == words


MASKED = [
    "password_hash", "PasswordHash", "session_token", "recovery_hash", "pin", "PINs",
    "APIKey", "api_keys", "OTPSecret", "password2", "users.password_hash",
    "access_key", "encryption_key", "verification_code", "one_time_code",
    # Known over-masks, pinned so the heuristic cannot drift silently.
    "token_count", "content_hash", "mfa_enabled", "password_changed_at",
]
NOT_MASKED = [
    "id", "name", "email", "phone", "last_login_ip", "session_expires", "Key",
    "COLUMN_KEY", "sort_key", "zip_code", "author_id",
    # Known under-masks: run-together words are not split, and an acronym
    # plural running into the next word splits inside the acronym.
    "passwordhash", "OTPsCount",
]


@pytest.mark.parametrize("label", MASKED)
def test_secret_labels_are_masked(label: str) -> None:
    assert SecretColumnMatcher().is_secret(label)


@pytest.mark.parametrize("label", NOT_MASKED)
def test_ordinary_labels_are_not_masked(label: str) -> None:
    assert not SecretColumnMatcher().is_secret(label)


def test_extra_words_split_by_the_label_rule() -> None:
    assert not SecretColumnMatcher().is_secret("PhoneNumber")
    assert SecretColumnMatcher(extra_words=["phone_number"]).is_secret("PhoneNumber")


def test_unmasked_columns_match_whole_label_case_insensitively() -> None:
    matcher = SecretColumnMatcher(unmasked_columns=["token_count"])
    assert not matcher.is_secret("TOKEN_COUNT")
    assert matcher.is_secret("users.token_count")
    assert matcher.is_secret("u.token_count")


def test_unmasked_columns_accept_a_one_shot_iterable() -> None:
    matcher = SecretColumnMatcher(unmasked_columns=iter(["token_count"]))
    assert not matcher.is_secret("token_count")


def test_unmasked_words_remove_only_that_builtin_entry() -> None:
    matcher = SecretColumnMatcher(unmasked_words=["Token"])
    assert not matcher.is_secret("SUM(output_tokens)")
    assert not matcher.is_secret("users.token_count")
    # Other built-in entries still apply, removable ones included.
    assert matcher.is_secret("password_token")
    assert matcher.is_secret("api_key")
    assert matcher.is_secret("content_hash")
    assert matcher.is_secret("pin")
    assert matcher.is_secret("mfa_enabled")


def test_extra_words_still_apply_after_their_builtin_word_is_removed() -> None:
    matcher = SecretColumnMatcher(unmasked_words=["token"], extra_words=["session token"])
    assert matcher.is_secret("session_token")
    assert not matcher.is_secret("token_count")


@pytest.mark.parametrize("entry", ["password", "secret", "api key", "tokens", "phone"])
def test_unmasked_words_reject_protected_or_unknown_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="not a removable built-in entry"):
        SecretColumnMatcher(unmasked_words=[entry])


def test_removable_entries_are_builtin_entries() -> None:
    assert set(REMOVABLE_BUILTIN_ENTRIES) < set(BUILTIN_SECRET_ENTRIES)
    # Removing every removable entry still masks the protected core.
    matcher = SecretColumnMatcher(unmasked_words=REMOVABLE_BUILTIN_ENTRIES)
    assert matcher.is_secret("password_hash")
    assert matcher.is_secret("client_secret")
    assert not matcher.is_secret("session_token")


# --- guard -----------------------------------------------------------------


def test_mask_replaces_every_value_including_null_and_keeps_label() -> None:
    df = pd.DataFrame({"id": [1, 2], "password_hash": ["h1", None]})
    out = mask_secret_columns(df, SecretColumnMatcher())
    assert list(out.columns) == ["id", "password_hash"]
    assert out["password_hash"].tolist() == [W, W]
    assert out["id"].tolist() == [1, 2]
    assert out.attrs[MASKED_COLUMNS_ATTR] == ["password_hash"]
    assert df["password_hash"].iloc[0] == "h1"
    assert df["password_hash"].isna().iloc[1]


def test_mask_leaves_frame_without_secret_column_unchanged() -> None:
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    out = mask_secret_columns(df, SecretColumnMatcher())
    assert out is df
    assert MASKED_COLUMNS_ATTR not in out.attrs


def test_mask_on_empty_frame_is_unchanged() -> None:
    df = pd.DataFrame()
    assert mask_secret_columns(df, SecretColumnMatcher()) is df


def test_mask_keeps_truncation_attrs() -> None:
    df = pd.DataFrame({"pin": ["1234"]})
    mark_truncation(df, truncated=True, max_rows=1)
    out = mask_secret_columns(df, SecretColumnMatcher())
    assert out.attrs["truncated"] is True
    assert out.attrs["max_rows"] == 1


def test_mask_handles_duplicate_labels() -> None:
    df = pd.DataFrame([[1, "a", "b"]], columns=["id", "token", "token"])
    out = mask_secret_columns(df, SecretColumnMatcher())
    assert out.iloc[0].tolist() == [1, W, W]


# --- run_sql sinks ---------------------------------------------------------

SENTINELS = {
    "password_hash": "SENTINEL-HASH",
    "session_token": "SENTINEL-TOKEN",
    "recovery_hash": "SENTINEL-RECOVERY",
    "pin": "SENTINEL-PIN",
}


class _FrameRunner(SqlRunner):
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def run_sql(self, args, context, *, require_base_tables: bool = False):  # type: ignore[no-untyped-def]
        return self._df.copy()


def _user_rows(n: int = 1) -> pd.DataFrame:
    row = {
        "id": 7, "name": "Ana Lopez", "email": "ana@example.com", "phone": "555",
        "password_hash": SENTINELS["password_hash"],
        "session_token": SENTINELS["session_token"],
        "session_expires": "2026-12-01",
        "recovery_hash": SENTINELS["recovery_hash"],
        "pin": SENTINELS["pin"],
        "last_login_ip": "10.0.0.1", "user_agent": "Firefox",
    }
    return pd.DataFrame([dict(row, id=i) for i in range(n)])


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="t"), conversation_id="c", request_id="r",
        agent_memory=StubAgentMemory(),
    )


def _run(tool: RunSqlTool, sql: str = "SELECT * FROM users"):  # type: ignore[no-untyped-def]
    return asyncio.run(tool.execute(_context(), RunSqlToolArgs(sql=sql)))


NOTE = (
    "SQL Lens withheld the values in these columns: password_hash, session_token, "
    "recovery_hash, pin. Tell the user these values are hidden; do not try to "
    "retrieve them with another query."
)


@pytest.mark.parametrize("rows", [1, 3])
def test_no_secret_value_reaches_any_run_sql_sink(tmp_path: Path, rows: int) -> None:
    runner = ResultMaskingRunner(_FrameRunner(_user_rows(rows)))
    tool = RunSqlTool(sql_runner=runner, file_system=LocalFileSystem(str(tmp_path)))
    result = _run(tool)

    assert result.success
    csv_files = list(tmp_path.rglob("*.csv"))
    assert len(csv_files) == 1
    markdown, _err, blocks, _qi, _mi = components_to_blocks([result.ui_component])
    sinks = {
        "llm": result.result_for_llm,
        "ui": result.ui_component.model_dump_json(),
        "metadata": json.dumps(result.metadata["results"]),
        "csv": csv_files[0].read_text(),
        "query_database": markdown + json.dumps(blocks),
    }
    for where, text in sinks.items():
        for secret in SENTINELS.values():
            assert secret not in text, f"{secret} leaked into {where}"
    for label in SENTINELS:
        assert label in sinks["llm"]
        assert all(r[label] == W for r in result.metadata["results"])
    assert "ana@example.com" in sinks["llm"]
    assert result.result_for_llm.endswith(NOTE)
    if rows == 1:
        assert "\\[withheld\\]" in markdown
    else:
        assert W in json.dumps(blocks)


def test_note_survives_long_preview_and_follows_truncation_note(tmp_path: Path) -> None:
    df = _user_rows(200)
    mark_truncation(df, truncated=True, max_rows=200)
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(df)),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    text = _run(tool).result_for_llm
    assert "(Results truncated to 1000 characters for preview.)" in text
    assert "Result truncated at 200 rows." in text
    assert text.endswith(NOTE)


def test_no_note_without_masked_column(tmp_path: Path) -> None:
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(df)),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    assert "withheld" not in _run(tool).result_for_llm


def test_no_note_for_empty_result(tmp_path: Path) -> None:
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(pd.DataFrame())),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    assert "withheld" not in _run(tool).result_for_llm


def test_no_note_for_zero_row_result_with_secret_column(tmp_path: Path) -> None:
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(_user_rows(1).iloc[0:0])),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    text = _run(tool).result_for_llm
    assert text.startswith("Query executed successfully. No rows returned.")
    assert "withheld" not in text


def test_guard_forwards_require_base_tables() -> None:
    seen: list[bool] = []

    class _Spy(SqlRunner):
        async def run_sql(self, args, context, *, require_base_tables: bool = False):  # type: ignore[no-untyped-def]
            seen.append(require_base_tables)
            return pd.DataFrame()

    guard = ResultMaskingRunner(_Spy())
    asyncio.run(guard.run_sql(RunSqlToolArgs(sql="SELECT 1"), _context()))
    asyncio.run(
        guard.run_sql(RunSqlToolArgs(sql="SELECT 1"), _context(), require_base_tables=True)
    )
    assert seen == [False, True]


def test_unexpected_masking_error_fails_closed_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Broken(SecretColumnMatcher):
        def is_secret(self, label: object) -> bool:
            raise RuntimeError("simulated matcher bug")

    guard = ResultMaskingRunner(_FrameRunner(_user_rows(1)))
    guard._matcher = _Broken()
    with caplog.at_level("WARNING", logger="sqllens.safety.masking"):
        with pytest.raises(ResultMaskingError, match="result masking guard errored"):
            asyncio.run(guard.run_sql(RunSqlToolArgs(sql="SELECT 1"), _context()))
    assert "result masking guard raised an unexpected RuntimeError" in caplog.text


def test_masking_error_reaches_run_sql_without_values(tmp_path: Path) -> None:
    class _Broken(SecretColumnMatcher):
        def is_secret(self, label: object) -> bool:
            raise RuntimeError("simulated matcher bug")

    guard = ResultMaskingRunner(_FrameRunner(_user_rows(1)))
    guard._matcher = _Broken()
    result = _run(RunSqlTool(sql_runner=guard, file_system=LocalFileSystem(str(tmp_path))))
    assert result.success is False
    assert "result masking guard errored" in result.result_for_llm
    assert not any(s in result.result_for_llm for s in SENTINELS.values())


def test_stale_masked_record_from_a_runner_beneath_is_dropped(tmp_path: Path) -> None:
    df = pd.DataFrame({"id": [1]})
    df.attrs[MASKED_COLUMNS_ATTR] = ["id"]
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(df)),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    result = _run(tool)
    assert result.success is True
    assert "withheld" not in result.result_for_llm
    # Control: the same frame with a real secret column still gets the note.
    secret = pd.DataFrame({"id": [1], "pin": ["1234"]})
    secret.attrs[MASKED_COLUMNS_ATTR] = ["id"]
    tool = RunSqlTool(
        sql_runner=ResultMaskingRunner(_FrameRunner(secret)),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    assert "withheld the values in these columns: pin." in _run(tool).result_for_llm


def test_matcher_settings_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        SecretColumnMatcher(["phone"])  # type: ignore[misc]


def test_relation_check_unwraps_the_masking_guard() -> None:
    engine = _FrameRunner(pd.DataFrame())
    wrapped = ResultMaskingRunner(RowCapRunner(engine, max_rows=10))
    assert _relation_check_executor(wrapped) is engine


# --- config ----------------------------------------------------------------


def test_absent_section_uses_builtin_list_with_no_exemptions() -> None:
    cfg = Config(database=DatabaseConfig(url="sqlite:///:memory:"))
    assert cfg.result_masking.extra_words == []
    assert cfg.result_masking.unmasked_columns == []


@pytest.mark.parametrize(
    ("section", "needle"),
    [
        ({"enabled": False}, "enabled"),
        ({"extra_words": "phone"}, "extra_words"),
        ({"unmasked_columns": [1]}, "unmasked_columns"),
        ({"extra_words": [""]}, "extra_words"),
        ({"extra_words": ["__"]}, "extra_words"),
        ({"unmasked_columns": [""]}, "unmasked_columns"),
        ({"unmasked_columns": ["!!!"]}, "unmasked_columns"),
        ({"unmasked_words": ["password"]}, "unmasked_words"),
        ({"unmasked_words": [""]}, "unmasked_words"),
        ({"unmasked_words": "token"}, "unmasked_words"),
    ],
)
def test_invalid_section_fails_naming_the_setting(section: dict, needle: str) -> None:
    with pytest.raises(ValidationError) as exc:
        ResultMaskingConfig(**section)
    assert needle in str(exc.value)


def _write_toml(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "sqllens.toml"
    path.write_text(
        '[database]\nurl = "sqlite:///:memory:"\n[llm]\napi_key = "sk-ant-x"\n' + extra
    )
    return path


def test_toml_section_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_toml(
        tmp_path,
        '[result_masking]\nextra_words = ["phone"]\nunmasked_columns = ["token_count"]\n',
    )
    cfg = Config.load(path)
    assert cfg.result_masking.extra_words == ["phone"]
    assert cfg.result_masking.unmasked_columns == ["token_count"]


def test_toml_unknown_key_fails(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "[result_masking]\nenable = false\n")
    with pytest.raises(ValidationError, match="enable"):
        Config.load(path)


def test_env_json_array_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_RESULT_MASKING__EXTRA_WORDS", '["phone"]')
    cfg = Config.load(_write_toml(tmp_path, ""))
    assert cfg.result_masking.extra_words == ["phone"]


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("EXTRA_WORDS", "3"),
        ("EXTRA_WORDS", '{"a": "b"}'),
        ("UNMASKED_COLUMNS", "[1]"),
        ("UNMASKED_WORDS", '"token"'),
    ],
)
def test_env_wrong_type_fails_naming_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    monkeypatch.setenv(f"SQLLENS_RESULT_MASKING__{setting}", value)
    with pytest.raises(ValidationError, match=setting.lower()):
        Config.load(_write_toml(tmp_path, ""))


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("EXTRA_WORDS", '[""]'),
        ("UNMASKED_COLUMNS", '["--"]'),
        ("UNMASKED_WORDS", '["secret"]'),
    ],
)
def test_env_invalid_entry_fails_naming_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    monkeypatch.setenv(f"SQLLENS_RESULT_MASKING__{setting}", value)
    with pytest.raises(ValidationError, match=setting.lower()):
        Config.load(_write_toml(tmp_path, ""))


def test_env_non_json_fails_naming_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_RESULT_MASKING__EXTRA_WORDS", "phone")
    with pytest.raises(Exception, match="result_masking"):
        Config.load(_write_toml(tmp_path, ""))


@pytest.mark.parametrize(
    ("toml_extra", "env"),
    [
        ("[result_masking]\nenable = false\n", None),
        ('[result_masking]\nextra_words = [""]\n', None),
        ("[result_masking]\nextra_words = 3\n", None),
        ("", '["", "x"]'),
        ("", "phone"),
    ],
)
def test_validate_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml_extra: str, env: str | None
) -> None:
    if env is not None:
        monkeypatch.setenv("SQLLENS_RESULT_MASKING__EXTRA_WORDS", env)
    path = _write_toml(tmp_path, toml_extra)
    result = CliRunner().invoke(app, ["validate", "-c", str(path)])
    assert result.exit_code != 0


# --- wiring ----------------------------------------------------------------

_RULE = RlsRule(table="users", column="id", value=1)


def _cfg(tmp_path: Path, *, read_only: bool, rls: bool, **masking) -> Config:  # type: ignore[no-untyped-def]
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:", read_only=read_only),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        rls=[_RULE] if rls else [],
        result_masking=ResultMaskingConfig(**masking),
    )


def _runner_of(agent) -> SqlRunner:  # type: ignore[no-untyped-def]
    tool = agent.tool_registry._tools["run_sql"]
    return getattr(tool, "_wrapped_tool", tool).sql_runner


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("rls", [True, False])
def test_masking_guard_is_outermost(tmp_path: Path, read_only: bool, rls: bool) -> None:
    runner = _runner_of(build_agent(_cfg(tmp_path, read_only=read_only, rls=rls)))
    assert type(runner) is ResultMaskingRunner
    inner = runner._inner
    expected = RlsGuardRunner if rls else (ReadOnlyGuardRunner if read_only else RowCapRunner)
    assert type(inner) is expected
    chain = []
    r: object = inner
    while r is not None:
        chain.append(type(r))
        r = getattr(r, "_inner", None)
    assert ResultMaskingRunner not in chain
    if rls:
        executor = _relation_check_executor(inner._inner)
        assert type(executor).__name__ == "SqliteRunner"


def test_config_reaches_the_guard(tmp_path: Path) -> None:
    runner = _runner_of(
        build_agent(
            _cfg(
                tmp_path, read_only=True, rls=False,
                extra_words=["phone"], unmasked_columns=["token_count"],
                unmasked_words=["hash"],
            )
        )
    )
    assert runner._matcher.is_secret("phone")
    assert not runner._matcher.is_secret("token_count")
    assert not runner._matcher.is_secret("commit_hash")
    assert runner._matcher.is_secret("password_hash")
    assert runner._matcher.is_secret("session_token")


# --- documented under-masks -------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["h", "row_to_json", "passwordhash", "email", "phone", "last_login_ip", "user_agent"],
)
def test_labels_without_an_entry_word_stay_unmasked(label: str) -> None:
    assert not SecretColumnMatcher().is_secret(label)


def test_sqlite_alias_hides_secret_but_unaliased_expression_is_masked(tmp_path: Path) -> None:
    import sqlite3

    from sqllens.agent.integrations.sqlite import SqliteRunner

    db = tmp_path / "u.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER, password_hash TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'SENTINEL-HASH')")
    conn.commit()
    conn.close()
    guard = ResultMaskingRunner(SqliteRunner(database_path=str(db)))

    aliased = asyncio.run(
        guard.run_sql(RunSqlToolArgs(sql="SELECT password_hash AS h FROM users"), _context())
    )
    assert aliased["h"].tolist() == ["SENTINEL-HASH"]
    expression = asyncio.run(
        guard.run_sql(RunSqlToolArgs(sql="SELECT upper(password_hash) FROM users"), _context())
    )
    assert expression.iloc[0, 0] == W
