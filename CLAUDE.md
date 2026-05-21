# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repo.

## Project overview

**SQL Lens** is a standalone MCP server that exposes a natural-language SQL agent and a vector memory store. It connects MCP-aware AI assistants (Cursor, Claude Desktop, Windsurf, custom clients) to a single configured database. Three tools are exposed:

- `query_database(question)` — NL → SQL → executed → Markdown table.
- `visualize_data(question)` — NL → SQL → ECharts chart payload + text answer.
- `list_data_sources()` — describes the configured database.

One database per running instance. Read-only by default, enforced by a `sqlglot` parser guard. Anthropic-only LLM in v1; the LLM interface is pluggable for future providers.

**License:** Apache 2.0. SPDX headers on all first-party `.py` files. Lifted code under `src/sqllens/agent/` is governed by `LICENSES/THIRD-PARTY.txt` — keep that file's copyright line intact.

**Repo:** https://github.com/The01Geek/sqllens (public, owner `The01Geek`).

This repo was extracted from a larger parent product (the maintainer knows the path). The extraction is intentional — SQL Lens is meant to stand on its own. Do **not** add parent-product-specific concerns (multi-tenancy, OAuth login UI, tenant settings JSONB, AILog persistence, RAG, chat skill routing) without an explicit decision recorded in an issue or design doc.

## Architecture

```
src/sqllens/
├── __init__.py              # __version__
├── __main__.py              # python -m sqllens
├── cli.py                   # Typer: version | init | validate | serve
├── config.py                # pydantic-settings: TOML + SQLLENS_ env vars
├── server.py                # FastMCP factory; dispatches to stdio or HTTP
├── tools/                   # MCP tool implementations (thin wrappers over agent/)
├── agent/                   # NL-to-SQL agent — lifted code (vendored, see below)
│   ├── factory.py           # build_agent / build_sql_runner — the only public seam
│   ├── core/, components/, capabilities/, integrations/, tools/, utils/
├── transport/               # ASGI layer
│   └── http.py              # Streamable HTTP + auth middleware + path normalizer
├── connectors/              # Reserved for SQLAlchemy-backed adapters (Phase 4+)
├── auth/                    # base (Authenticator protocol) + none | bearer | jwt
└── safety/                  # readonly.py SQL parser; ReadOnlyGuardRunner decorator
```

Layering: `cli → server → tools → agent.factory → agent.*`. The HTTP transport in `transport/http.py` wraps `server.build_server()` with auth + path normalization. Auth and safety are cross-cutting and may be imported anywhere.

The agent's `send_message` returns an async stream of `UiComponent` objects; `tools/_format.py` collapses that stream into a single Markdown string for the MCP tool result.

## The pruning choice (lifted agent code)

When extracting `sqllens.agent` we made a deliberate **aggressive-pruning** choice: copy only the modules transitively required by the MCP tools we ship, rather than carry the entire upstream framework. The original tree had 283 Python files; we kept ~110. v0.2 reintroduces a chart-emitting tool as a first-party `EmitChartTool` (`src/sqllens/agent/tools/emit_chart.py`) — not lifted from upstream — to back the `visualize_data` MCP tool.

**What this means for debugging:**

- If you hit unexpected behavior in the agent — wrong prompt, missing capability, unfamiliar code path, broken integration — **check the upstream source first** before assuming it's our bug. The reference copy lives on disk in the parent project we extracted from (the maintainer knows the path).
- Useful directories in the upstream to consult: `core/agent/`, `tools/`, `components/`, `integrations/anthropic/`, `integrations/chromadb/`, `integrations/postgres/`, `capabilities/`. We pruned `examples/`, `legacy/`, `web_components/`, `servers/`, most of `agents/`, 24 unused integration backends, and three of five upstream tools (upstream's visualize_data, python, file_system). The chart capability has since been reintroduced as the first-party `EmitChartTool` described above; the other two remain pruned.
- If a module we *did* copy references something we *didn't*, the import will fail at startup. The fix is usually one of: copy the missing module, replace it with a stub, or remove the dependency by simplifying our caller.

**Upstream brand cleanliness — strict rule:**

The agent code originated as a fork of an MIT-licensed upstream project. The legal copyright line lives in `LICENSES/THIRD-PARTY.txt` and **must not be removed**. Outside that one file, **no reference to the upstream's name may appear anywhere in the repository** — not in docs, not in code, not in module docstrings, not in user-facing strings (CLI help, MCP tool descriptions, log messages, system prompts, error messages). When lifting more code from upstream, sed-rewrite the upstream package namespace (the maintainer knows it) → `sqllens.agent.*` and scrub any upstream brand-name string occurrences in the same change.

## Commands

```bash
# Install (editable, with dev + all DB drivers)
pip install -e ".[dev,all]"

# Lint + tests (default skips connector tests that need real DBs)
ruff check .
pytest -q

# Connector tests (need SQLLENS_TEST_POSTGRES_URL + SQLLENS_TEST_MYSQL_URL)
pytest -q -m connectors

# Run the server (after `sqllens init` writes a sqllens.toml)
sqllens serve

# Validate a config without starting
sqllens validate -c sqllens.toml

# Build wheel + sdist
python -m build
```

Python 3.11+ required. Config can come from `./sqllens.toml`, `--config <path>`, or `SQLLENS_*` env vars. Nested fields use double-underscore: `SQLLENS_LLM__API_KEY=sk-ant-...`. Env wins over TOML.

## Code style

- Ruff with `E F I B UP RUF` selected, line length 100. `src/sqllens/agent/` is excluded — it's vendored.
- Type hints on every public signature.
- No new top-level dependencies without discussion.
- Tools (in `tools/`) are thin: parse args → call agent → format result. Business logic belongs in `agent/`.
- **Failures must surface as structured signal — never as a silent success or an unguarded crash.** Errors visible to MCP clients must be returned as `isError: true` with a clear message. Do **not** let the LLM apologize inside a tool result; the calling agent needs structured signal. This is the single most-violated rule in this repo — it has recurred across config, transport, and memory work. It covers four cases the terse "return errors as isError" wording kept missing:
  - **Partial failure is failure.** An operation that completed some work and failed the rest (e.g. `import_memory` with `saved == 0` and `errors > 0`, or any `errors > 0`) must be `isError: true`, not a success object that merely *reports* the error count. If the operation did not fully achieve what its name promises, it errored.
  - **Lossy / empty success needs a loud warning, not green output.** Bulk read/write operations (export-memory, import-memory) on an empty, corrupt, or version-skewed store must emit an explicit `Warning:` and never print unqualified success — a clean "Done" on a destroyed store is a data-loss trap.
  - **No unguarded dereference of optional/secret config on a non-CLI path.** Values like `cfg.llm.api_key` are `None` for HTTP-transport and programmatic callers; dereferencing (`.get_secret_value()`) without a guard raises a bare `AttributeError` that reaches the MCP client as an unstructured crash. Guard with a clear message (the `API_KEY_MISSING_MESSAGE` pattern) before dereferencing.
  - **No blanket `except` that swallows rows or steps.** A broad `except (TypeError, ValueError, ...): continue` over an iterator (e.g. `iter_all`) silently drops every record on schema/version skew and reports the result as a successful empty export. Catch narrowly, count what you skip, and surface a non-success if anything was skipped.

  Before returning success from any tool or CLI command, ask: *did every item this operation promised actually complete, and would a `None`/empty/corrupt input reach this return as a green result?* If the answer is uncertain, it is an error.
- New first-party `.py` files get SPDX headers:
  ```
  # SPDX-FileCopyrightText: 2026 Daniel Radman
  # SPDX-License-Identifier: Apache-2.0
  ```

## Claims must match the code (no fabricated behavior)

Every sentence that **describes runtime behavior** — in a docstring, an internal doc (`docs/internal/`), an external doc (`docs/external/`), a release note, a PR body, or a code comment — must describe what the code on this branch **actually does**, not what the issue asked for or what you intend to do next. Writing the doc first and the code second is how SQL Lens has shipped false claims twice (#117, #136).

Before you write or keep any behavioral claim, **trace it to the line that implements it**:

- **Boot vs. lazy / first-call.** If a doc says work happens "at startup" / "at `serve` boot," confirm the constructor (or warmup hook) actually performs that I/O — not just wires objects whose `__init__` is lazy. `build_agent` constructing a `ChromaAgentMemory` does *not* trigger the embedding-model download; only a real query/`get_recent_memories` touch does (#117).
- **Guard / validation scope.** If a doc claims an input is "blocked," "rejected," or "validated," find the branch that rejects it. Match the doc to the *actual* predicate: don't claim type-mismatch blocking, empty-string blocking, or load-time rejection unless the code does exactly that (#136 claimed RLS type-mismatch and load-time-reject behavior the guard never implemented; empty-string slipped through `_is_suspicious_scalar`).
- **Exception breadth.** A docstring promising "fail-secure to `{}`" must catch the exceptions that can actually be raised, not a narrower subset (#136 caught only `(ValueError, AttributeError)`).
- **Counting claims.** "Three orthogonal protections" / "two halves" / "N tools" must equal what the file now contains after your change. Adding a fourth guard means the count updates (#136 left "Three orthogonal protections" after adding `RlsGuardRunner`).
- **Sibling surfaces.** If two tools share a wrapped runner (`query_database` + `visualize_data`), a claim about "every query" must hold for both, or scope the claim and record the gap as an explicit follow-up — do not let the doc imply coverage the sibling lacks.

When a claim cannot yet be backed by code, either cut it or write it as an explicit "not yet / follow-up," and call that out in the PR body. A claim you can't trace to a line is a defect, not a placeholder.

## Release & distribution

Three artifact types, all driven by tag pushes (`vX.Y.Z`):

| Path | Trigger | Workflow | Where it lands |
|---|---|---|---|
| **PyPI** | tag `v*` | `release.yml` | https://pypi.org/project/sqllens/ via OIDC Trusted Publishing — no API tokens. The `pypi` GitHub environment exists; the matching pending publisher is configured on PyPI (project=`sqllens`, owner=`The01Geek`, workflow=`release.yml`, environment=`pypi`). |
| **Docker** | tag `v*`, push to `main` | `docker.yml` | `ghcr.io/the01geek/sqllens:{X.Y.Z, X.Y, latest}` for tags; `:edge` and `:git-<sha>` for main. Multi-arch (amd64 + arm64), cosign-signed (keyless OIDC), SBOM (SPDX) and build provenance attached. |
| **MCPB** | tag `v*` | `mcpb.yml` | Per-platform `.mcpb` bundles (macOS x86_64/arm64, Linux x86_64, Win32 x86_64) attached as GitHub Release assets. |

**Cutting a release:**
```bash
# Bump version in pyproject.toml first; release.yml's "Verify tag matches" step rejects mismatches.
git tag v0.1.0 && git push origin v0.1.0
```
PyPI versions are immutable after publish — you can yank but not re-upload the same version. Pre-release with `v0.1.0a1` if uncertain. The `softprops/action-gh-release` step on `release.yml` flags any tag containing `-` as a GitHub pre-release automatically.

## Repo conventions

- **`main` is protected.** Direct pushes are blocked. The ruleset (`main-protected`, ID 15633058) blocks force pushes and deletions, and these required status checks must pass before merge:
  - `Lint + unit + transport (py3.12)`
  - `Devflow Review` — the `/devflow:review` verdict gate. `devflow-review.yml`'s `finalize_check` concludes this check `failure` on a **REJECT** verdict (read from the formal `--request-changes` review, or the `## Verdict: REJECT` report line on the comment fallback), `success` on APPROVE, and `neutral` when the review was superseded/cancelled/intentionally not run (a neutral required check does not block). A REJECT therefore blocks auto-merge until the finding is addressed and the check's **Re-run** button is clicked (or `@claude run /devflow:review` re-reviews). Because it is required, **the review must remain enabled in `project-config.yml`** — disabling it while it is a required context would block every PR.
  - `Lint + unit + transport (py3.11)` and `Connector tests (Postgres + MySQL)` run on every PR but are **not** currently in the ruleset's required set (only `py3.12` is). Treat them as must-pass by convention; promoting them to required is a separate ruleset change.
- **No bypass actors** — even the owner cannot push directly. Use a feature branch + PR every time:
  ```bash
  git checkout -b fix/short-description
  # ...edits...
  git commit -s && git push origin fix/short-description
  gh pr create --fill
  gh pr merge --squash --delete-branch --auto    # auto-merge once CI passes
  ```
- **CI status check names** in code are load-bearing — if you rename a workflow job, also update the ruleset (`gh api repos/The01Geek/sqllens/rulesets/15633058`).
- **Workflow file names matter.** Avoid renaming `release.yml` — the PyPI Trusted Publisher is bound to that filename.

## What not to add

- Multi-tenancy. One database per running instance. If you need many, run many servers.
- A user model, login flow, or session storage. Authentication is delegated to upstream IdPs (JWT) or static bearer tokens.
- A document RAG pipeline. SQL Lens is SQL-only.
- A web UI. The MCP transport is the UI.
- Schema migrations / a server-side database. ChromaDB is the only persistent store, on the local filesystem.

## Gotchas (things that have bitten us)

- **GitHub Actions YAML and bash line continuations.** A `run: |` block scalar requires every line indented. Bash `\` line continuations whose next line starts at column 1 are parsed by YAML as new top-level keys (without colons) and reject the whole workflow file at validation. Use heredocs or single-line forms instead.
- **Unanchored `.gitignore` entries match anywhere.** A bare `data/` rule silently ignored `src/sqllens/agent/components/rich/data/`, breaking CI. Anchor local-data ignores with a leading slash (`/data/`, `/chroma/`).
- **OCI image references must be all lowercase.** `${{ github.repository_owner }}` preserves GitHub's case — buildx tolerates uppercase with a warning, but syft (SBOM) and cosign reject it. The Docker workflow lowercases via bash `${VAR,,}` and re-exports through `$GITHUB_ENV`.
- **Docker `--network=host` on Docker Desktop / WSL2** puts the container in Docker Desktop's internal WSL distro, which is a *different* network namespace than the user's WSL. Native processes in user-WSL (curl, MCP Inspector, IDEs) can't reach `127.0.0.1:<port>` on the container even though sibling containers can. **Always use port mapping** (`-p HOST:CONTAINER`) for local dev unless you specifically need host-shared networking.
- **FastMCP rejects non-loopback `Host` headers** by default ("421 Misdirected Request"). When connecting from a docker container to `host.docker.internal:<port>`, the inbound `Host` header is `host.docker.internal:<port>` and gets rejected. Either bind from the same network namespace (so `127.0.0.1` resolves locally) or configure FastMCP's transport security if exposing remotely.

## Debugging checklist

1. Reproduce against the bundled SQLite demo first. If it fails there, the issue is local; if not, the user's database/config is involved.
2. If the agent runs out of tool iterations exploring schema (especially against an untrained DB), the configured `max_tool_iterations` may be too low — or the agent has no memory yet. ChromaDB needs time to build, and the first query downloads ~80 MB of embedding model.
3. If the answer is wrong, look at memory hits — `SQLLENS_MEMORY__SIMILARITY_THRESHOLD` may be too high or too low.
4. If a tool errors out in an unfamiliar way, **diff our agent file against the upstream source** to see if we missed a code path during the lift.
5. For MCP transport issues, escalate slowly: curl first (raw JSON-RPC), then MCP Inspector, then the IDE. Each layer adds its own failure modes.
