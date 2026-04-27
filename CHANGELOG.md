# Changelog

All notable changes to SQL Lens will be documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [SemVer](https://semver.org/) once it reaches `0.1.0`.

## [Unreleased]

### Added
- Initial repository scaffold (Phase 1 spike).
- NL-to-SQL agent in `sqllens.agent`, pruned to 110 files.
- Authentication module (`auth.none`, `auth.bearer`); `auth.jwt` scaffolded.
- Read-only SQL guard via sqlglot — refuses any non-SELECT statement.
- Streamable HTTP transport with auth middleware and trailing-slash
  path normalizer.
- Integration tests over the live MCP wire protocol using the `mcp` SDK
  client and a real uvicorn server. 39 tests total.
- IDE config snippets for Cursor, Claude Desktop, Windsurf, and stdio.
- Multi-stage Dockerfile producing a slim non-root runtime image.
- Release pipeline: PyPI Trusted Publishing + GHCR multi-arch Docker
  (linux/amd64, linux/arm64) with cosign signatures and SBOM.
- MCPB bundle for Claude Desktop drag-and-drop install. Per-platform
  builds for macOS x86_64/arm64, Linux x86_64, and Windows x86_64.
- Connector integration tests against real Postgres + MySQL instances,
  running under GitHub Actions service containers.
