# Named config profiles (per-request settings)

This page explains how one running server can answer different requests with different settings. It is for anyone changing `src/sqllens/profiles.py`, `src/sqllens/runtime.py`, the profile-admin tools, or any code that reads one of the four profile settings.

## What a profile is

A profile is a named set of values for four settings that shape a `query_database` result:

| Profile field | Base config it overrides |
|---|---|
| `show_details` | `agent.show_details` |
| `max_tool_iterations` | `agent.max_tool_iterations` |
| `max_rows` | `database.max_rows` |
| `similarity_threshold` | `memory.similarity_threshold` |

The model is `Profile` in `src/sqllens/profiles.py`. Every field can be `None`. A `None` field means "use the base config value". The model sets `extra="forbid"`, so a misspelled field fails validation instead of being dropped. The field bounds copy the bounds on the base config fields, so a profile cannot set a value the base config would refuse.

Profiles are not part of `Config`. They live in a JSON file, `sqllens.profiles.json`, inside `memory.persist_dir` (the same directory as the ChromaDB store). The only profile setting in `Config` is `ProfilesConfig.allow_admin_tools` in `src/sqllens/config.py`, the switch for the admin tools described below.

## How a request picks its settings

The caller passes an optional `profile` argument to `query_database`. The call path is:

1. `query_database` in `src/sqllens/server.py` passes `profile` and the shared `ProfileStore` to `query_database_impl_with_widgets`.
2. `_query_database_turn` in `src/sqllens/tools/query_database.py` calls `resolve_effective_settings(cfg, profile, store=profile_store)`.
3. `resolve_effective_settings` returns an `EffectiveSettings`: a frozen dataclass in `src/sqllens/runtime.py` whose four fields always hold concrete values.
4. `set_effective_settings` publishes that object on a `ContextVar`. The `finally` block of `_query_database_turn` calls `reset_effective_settings` with the saved token.

Name resolution in `resolve_effective_settings`:

- A missing or empty name means the reserved `default` profile.
- A name that is not a valid identifier (1 to 64 characters from `[A-Za-z0-9_.-]`, checked by `validate_profile_name`) raises `ProfileNameError` before the agent runs.
- A valid name with no stored profile falls back to the stored `default` profile.
- If there is no stored `default` profile, every field comes from the base config.

A `ContextVar` is used because the agent is one process-wide object shared by all requests (see `tools/_agent.py`). The `ContextVar` lets two concurrent requests use different values without rebuilding or changing the shared agent.

## Who reads the effective settings

`get_effective_settings()` returns `None` outside a request (for example during boot warmup, in the CLI, or in tests). Every reader below then uses its own base value.

- `effective_row_cap` in `src/sqllens/safety/limits.py` returns `min(constructor_cap, effective.max_rows)`. A profile can lower the row cap for a request. It cannot raise it above `database.max_rows`.
- The agent run loop in `src/sqllens/agent/core/agent/agent.py` uses `min(self.config.max_tool_iterations, effective.max_tool_iterations)`. A profile can lower the iteration limit. It cannot raise it.
- The memory search tool in `src/sqllens/agent/tools/agent_memory.py` and the system-prompt context enhancer in `src/sqllens/agent/core/enhancer/default.py` use `effective.similarity_threshold` when no explicit threshold is given.
- `src/sqllens/tools/query_database.py` uses `effective.show_details` to decide whether the executed SQL and the agent trace are returned.

The two "lower only" rules matter. A profile changes `max_rows` and `max_tool_iterations` only in the downward direction, even though `upsert_profile` accepts any value inside the field bounds.

## The profile store

`ProfileStore` in `src/sqllens/profiles.py` reads the JSON file once, in its constructor, and serves every read from memory. `build_server` in `src/sqllens/server.py` builds one store at startup and shares it between `query_database` and the admin tools.

Writes:

- `upsert` and `delete` hold an `asyncio.Lock`, change the in-memory copy, then write the file with `atomic_write_text` from `src/sqllens/_atomic.py` on a worker thread.
- If the file write fails, the in-memory change is undone, so memory and disk stay the same.

Most load failures leave the store degraded and flagged, instead of reporting an empty store as success:

- A file that cannot be read (an `OSError`), invalid JSON, or a top-level value that is not an object sets `load_error` to a message that starts with `Warning:`, logs it, and leaves the store with no profiles. The bad file stays on disk.
- A single entry that fails validation is skipped and logged on its own line. When every entry name is valid, `load_error` names every skipped entry.
- An entry whose name fails `validate_profile_name` is skipped, and all writes are refused until an operator renames it in the file and restarts. This stops a save from deleting the entries that were skipped. In this case `load_error` reports only the count of invalid names. Any entries skipped for a bad body are named only in their per-entry log lines.

One failure is not handled this way. A file that exists but is not valid UTF-8 raises `UnicodeDecodeError` from `read_text`. Only `OSError` is caught around that call, so the error escapes the `ProfileStore` constructor and stops `build_server`.

## The profile-admin tools

When `profiles.allow_admin_tools` is true, `build_server` registers four tools and the config-admin widget (`src/sqllens/ui/config_admin.html`):

| Tool | Writes? | Notes |
|---|---|---|
| `list_profiles` | No | Returns the stored profiles, the base values, and the per-field bounds. If `load_error` is set, it returns the same body as an `isError` result with a `warning` field. |
| `get_profile` | No | An unknown name is an `isError` result, not a `null`. A store with `load_error` set returns a "degraded state" `isError` result. |
| `upsert_profile` | Yes | Validates the body with `Profile.model_validate`; an invalid body is an `isError` result. |
| `delete_profile` | Yes | Refuses the reserved `default` name and unknown names with `isError`. |

The two write tools call `_require_write_auth("profile")`, the same check `import_memory` and the memory-admin write tools use. It refuses to run when `auth.mode` is `none` unless `auth.insecure` is true.

## Other callers

`verification_config` in `src/sqllens/eval/runner.py` (used by `sqllens verify-memory`) resolves the stored `default` profile with `resolve_effective_settings(cfg, None, store=ProfileStore(cfg))` and builds a copy of the config from it. The copy takes `similarity_threshold` from the profile. It takes `max_tool_iterations` and `max_rows` as the lower of the base value and the profile value. It always sets `show_details` to true and turns off both memory-saving settings (`save_queries` and `save_text_memories`). The stored profile and the caller's config are not changed.

## Tests

- `tests/unit/test_profiles.py` covers the store, the resolution rules and the admin tools.
- `tests/unit/test_profile_name_boundaries.py` and `tests/integration/test_profile_name_boundaries.py` cover profile-name validation, the second one at the MCP boundary.
