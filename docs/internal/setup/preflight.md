# Eager preflight on `sqllens serve`

Source-of-truth reference for [src/sqllens/preflight.py](../../../src/sqllens/preflight.py) and its integration into the [CLI](../../../src/sqllens/cli.py). Tracks production-readiness item **S-4** in [production-readiness-v0.1.0.md](../production-readiness-v0.1.0.md).

## Why preflight exists

Before this module, infrastructure misconfiguration (bad DSN, missing API key, unwritable Chroma persist dir, bearer mode with no token) was deferred until the first `query_database` MCP call. There it landed inside the agent's blanket exception handler, which collapses every internal error into a generic "Please try again" message in the MCP client. Operators had no startup signal to fail fast on, and the real driver error sat only in stderr.

`run_preflight(cfg)` runs four probes immediately after `Config.load()` in `sqllens serve` and exits with code 2 and a single-line `Preflight failed: <subsystem>: <detail>` message if any of them fails. That puts the fail-fast guard before the transport binds the port — operators see a clear, actionable error before any MCP client connects.

## Public surface

```python
from sqllens.preflight import (
    PreflightError,
    probe_database,
    probe_llm,
    probe_memory,
    probe_auth,
    run_preflight,
)
```

`PreflightError(subsystem, detail)` exposes `.subsystem` (`Literal["database", "llm", "memory", "auth"]`) and `.detail` (the underlying message). Its `__str__` is `f"{subsystem}: {detail}"`, which the CLI prefixes with `Preflight failed:` for the user-facing line.

Each `probe_*` accepts a fully-loaded `Config` and returns `None` on success, raising `PreflightError` otherwise. The originating driver exception is chained via `__cause__` so callers that re-raise (or `pytest -s` runs) still see the full traceback.

### Exception-narrowing contract

Probes catch **only the driver / SDK exception base** for the subsystem they validate and re-label it as a `PreflightError`. Anything else — `TypeError`, `AttributeError`, an `ImportError` from a missing transitive dep, or any other programmer error — propagates as itself. The point is to avoid masking bugs by re-labelling them as "database failure" or "llm failure"; a `TypeError` from preflight should surface as a `TypeError`, not as `database: TypeError: ...`.

| Probe | Caught (re-labelled as `PreflightError`) | Propagated as-is |
|---|---|---|
| `probe_database` (sqlite) | `sqlite3.Error` | everything else |
| `probe_database` (postgres) | `psycopg2.Error` | everything else (the explicit `ImportError` branch above this catch handles the "driver not installed" case) |
| `probe_database` (mysql) | `pymysql.MySQLError` | everything else (same: the explicit `ImportError` branch handles a missing driver) |
| `probe_llm` | `anthropic.AnthropicError` | everything else, including `ImportError` from `import anthropic` or `from sqllens.agent.integrations import AnthropicLlmService` — these imports are deliberately kept **outside** the `try` block so a packaging breakage doesn't masquerade as an "llm" subsystem failure |
| `probe_auth` | `ValueError` (and a residual `Exception` catch for parity with `build_authenticator`'s historic surface) | n/a |

`run_preflight(cfg)` runs the four probes in a fixed order — `database → llm → memory → auth` — and short-circuits at the first failure. Ordering is most-likely-to-fail first: DSN typos and missing API keys are the common operator mistakes; the Chroma persist dir and auth mode rarely change.

## What each probe does (and doesn't do)

### `probe_database`

Opens and immediately closes a connection to `cfg.database.url`. **Does not run a query** — that would burn a round-trip and could trigger permission errors unrelated to reachability.

Scheme handling:

| URL prefix | Driver | Timeout |
|---|---|---|
| `sqlite://`, `sqlite+...://` | `sqlite3.connect(path, timeout=5)` | `timeout` is lock-wait, **not** connect-timeout. The `open()` call is the only blocking step and is effectively instant for local files. |
| `postgres://`, `postgresql://`, `postgresql+...://` | `psycopg2.connect(normalized, connect_timeout=5)` | psycopg2 only accepts the `postgresql://` spelling; the probe collapses the legacy and SQLAlchemy-style aliases before connecting. |
| `mysql://`, `mysql+...://` | `pymysql.connect(connect_timeout=5)` | URL is parsed with `urllib.parse.urlparse` and decomposed into `host/port/user/password/database`. Missing user or host raises a `PreflightError` before connecting. Absent port defaults to `3306`, absent password to `""`, absent path to `""`. |

A URL with no `://` separator, or with an unsupported scheme, fails fast with a clear message rather than reaching the driver.

The 5-second connect timeout (`_DB_CONNECT_TIMEOUT_SECONDS`) bounds how long a wedged host can extend startup. SQLite gets the same value passed for completeness even though it doesn't apply — the file open is unbounded on a wedged remote mount, but operators using SQLite over NFS already know what they signed up for.

### `probe_llm`

Constructs `AnthropicLlmService(model=cfg.llm.model, api_key=cfg.llm.api_key.get_secret_value())`. Goes through the service class rather than `anthropic.Anthropic` directly so the `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` env-var fallback and the model-default behavior match what [`build_agent`](../agent/factory.md) will instantiate at serve time.

**No network round-trip.** A real `messages.create` would cost a billed token and slow restarts. Construction alone validates the constructor signature and surfaces obviously-broken keys (the SDK rejects empty strings at construction).

If `cfg.llm.api_key is None`, the probe raises `PreflightError("llm", API_KEY_MISSING_MESSAGE)` — same message the CLI's earlier `cfg.llm.api_key is None` gate uses. The earlier gate stays in place because it runs whether or not preflight is enabled.

### `probe_memory`

Creates `cfg.memory.persist_dir` (with parents) and confirms writability by touching and removing a sentinel file (`.sqllens-preflight`). Uses an actual touch rather than `os.access` because `os.access` gives wrong answers under EUID/ACL setups and races with the real write.

**Does not open a Chroma collection.** That would trigger the ~80 MB embedding-model download on first run (see [agent/memory.md](../agent/memory.md)), which by design stays lazy — the first `query_database` call already pays that cost and we don't want to repeat it on every restart.

The sentinel-removal is wrapped in `contextlib.suppress(OSError)` so a permission flip between `touch` and `unlink` can't shadow the real probe result. If the touch succeeded, the directory is writable; cleanup noise is irrelevant.

### `probe_auth`

Calls `build_authenticator(cfg.auth)`. The function raises `ValueError` with an actionable message (e.g. `auth.mode='bearer' requires auth.bearer_token to be set`) when its preconditions aren't met. The probe catches `ValueError` specifically and passes the message through unmodified — prefixing it with `ValueError:` would make a config oversight read like an internal bug.

For stdio mode this is the only place the auth config is exercised before the first request. A `mode = "bearer"` config with no token (silent footgun in stdio today) now fails at startup instead of going undetected.

## CLI integration

### `sqllens serve`

Runs `run_preflight(cfg)` after `Config.load` and the `cfg.llm.api_key is None` gate, before `run(cfg)`. On `PreflightError`:

```
Preflight failed: <subsystem>: <detail>
```

Exit code 2.

`--no-preflight` (or `SQLLENS_NO_PREFLIGHT=1`) skips all four probes. The skip is announced in yellow:

```
Preflight skipped (--no-preflight).
```

This is intentional UX — operators in container orchestrators where dependencies come up after the server (init-container patterns, sidecars, late-binding DSN) need the escape hatch, but losing the safety net silently is worse than the orchestration friction it removes.

### `sqllens validate`

Schema validation is always run. Four opt-in flags select preflight probes:

| Flag | Runs |
|---|---|
| `--check-db` | `probe_database` |
| `--check-llm` | `probe_llm` |
| `--check-memory` | `probe_memory` |
| `--check-auth` | `probe_auth` |

Each selected probe prints `<label> OK` in green on success, or `Preflight failed: <subsystem>: <detail>` and exits 2 on failure. The probes run in the order the flags appear in the source (db → llm → memory → auth) — same as `run_preflight`.

`validate` without any `--check-*` flag remains structural-only, preserving its existing CI-friendly contract (no DB, no LLM, no Chroma I/O).

## Error message format

All preflight failures share the format:

```
Preflight failed: <subsystem>: <detail>
```

Where `<subsystem>` is one of `database | llm | memory | auth` and `<detail>` is either:

- A driver exception rendered as `f"{type(exc).__name__}: {exc}"` (database probe).
- The constructor exception rendered the same way (llm probe).
- A short cause string for filesystem failures (memory probe).
- The raw `ValueError.args[0]` from `build_authenticator` (auth probe).

Driver exception strings can leak DSN-derived hints (host, port, database name) — S-10 in [production-readiness-v0.1.0.md](../production-readiness-v0.1.0.md) tracks the broader question of what error detail to expose. For preflight the trade-off is intentional: the operator running `serve` is the one who set the DSN, so the leaked hint goes to a principal who already has the secret.

## What preflight is *not*

- **Not a query check.** It verifies reachability and config validity, not that the role can `SELECT` anything specific. A connection-OK database with zero table grants will still surface `permission denied` from the agent at query time.
- **Not a port-binding check.** The transport binds the port after preflight passes. A port collision still surfaces as a uvicorn error a moment later — separate failure surface.
- **Not a replacement for the agent's blanket exception handler.** That handler still wraps tool execution; preflight only addresses the *startup* surface. Replacing the handler is tracked separately (see S-4's "Out of scope" note in the issue).

## Adding a new probe

1. Write a `probe_<thing>(cfg: Config) -> None` function in `preflight.py` following the existing pattern: do the bounded check, raise `PreflightError("<thing>", detail)` on failure with `from exc`. **Catch only the driver/SDK exception base** for the subsystem (see the exception-narrowing contract above) — let `TypeError`/`AttributeError`/`ImportError` propagate so programmer errors don't get re-labelled as subsystem failures. If the subsystem's driver is an optional dependency, do the `import` in its own `try`/`except ImportError` block *before* the connectivity-check `try` block, so a missing-driver case surfaces with an actionable "install hint" message rather than as a generic propagated `ImportError`.
2. Add the new subsystem name to the `Subsystem` `Literal`.
3. Append the probe to the `_PROBES` tuple in the order it should run (cheapest / most-likely-to-fail first).
4. Add a `--check-<thing>` flag on `validate` in [cli.py](../../../src/sqllens/cli.py).
5. Cover the success path, the failure path, and the chained-exception path in [tests/unit/test_preflight.py](../../../tests/unit/test_preflight.py), and the CLI integration in [tests/unit/test_cli.py](../../../tests/unit/test_cli.py).
