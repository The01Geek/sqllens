# Release Notes

This page lists user-visible changes in each released version of SQL Lens. For the full developer-facing changelog, see `CHANGELOG.md` in the repository.

## May 16, 2026

- **[Fix] Tool errors now surface verbatim instead of being paraphrased** — When a tool call fails, SQL Lens now quotes the underlying error message in a fenced code block and asks how you want to proceed, instead of inventing a plausible-sounding root cause. This makes failures, such as access-denied errors on per-query scratch files, directly debuggable rather than misleading. (#20)

## Unreleased

- **Fix: Access-denied errors on every query under Claude Desktop on Windows** — SQL Lens previously wrote per-query scratch CSVs into its current working directory, which under Claude Desktop on Windows is the launcher's install folder and is not writable by the user. Every query failed with `[WinError 5] Access is denied`. Scratch files are now written into your user temp directory regardless of how the server is launched, so Claude Desktop on Windows works out of the box without the `.cmd` wrapper workaround. (#21)

## 0.0.2 — 2026-04-28

This release fixed two release-pipeline issues that affected the first published version.

- The Docker image for version 0.0.1 was not published to GHCR because the release workflow lacked the permission required to attach the software bill of materials. The 0.0.2 Docker image is available and signed.
- The MCPB bundle is now built from the current directory rather than a `file://` URL. This fixes an install failure on Windows builders where Python rejected the MSYS-style path.

## 0.0.1 — 2026-04-28

Initial public release.

### Features

- A natural-language SQL agent that translates questions to SQL, executes them, and returns results as a Markdown table.
- Two authentication modes for the HTTP transport: `none` and `bearer`. JWT support is scaffolded but not yet implemented.
- A read-only SQL guard that parses the generated query and rejects anything that is not a `SELECT`.
- A Streamable HTTP transport with authentication middleware and a path normalizer that accepts both `/mcp` and `/mcp/`.
- Drop-in MCP client configuration snippets for Cursor, Claude Desktop, Windsurf, and stdio launchers.

### Install paths

Three install paths are produced by the same release pipeline:

- **PyPI**: `pip install sqllens[all]` then `sqllens serve`.
- **Docker**: `docker run` against the published GHCR image. The image supports both `amd64` and `arm64` and is signed with cosign. A software bill of materials is attached to each release.
- **MCPB**: Drag the platform-specific bundle onto Claude Desktop for a one-click install. Builds are available for macOS (x86_64 and arm64), Linux (x86_64), and Windows (x86_64).

## See also

- **[Getting started](getting-started.md)** to install the latest release.
- **[Configuration reference](configuration.md)** for the current set of configuration fields.
