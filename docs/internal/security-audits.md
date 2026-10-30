# Private security audits

The weekly public workflow sends only a public commit SHA and a lookback window to
an explicitly configured private repository. Analysis runs in that private
repository; confirmed candidates become **draft repository security advisories**
in `The01Geek/sqllens`. Maintainers validate each draft and decide whether and when
to publish it. This automation never publishes advisories or opens public issues.

## Activation is a separate maintainer step

The private host and credentials have not been selected or provisioned by this
change. Until configured, the public dispatcher fails with a generic configuration
error and does not run an audit. It has no public-report fallback. To pause the
schedule explicitly, set `workflows.security_audit` to `false` in
`.prflow/config.json`; a non-Boolean value fails validation.

1. Choose a **private** GitHub repository with Actions enabled. Limit its readers
   and Actions log retention to the people and duration needed for security work.
   The analyzer may emit findings or error details into that repository's logs.
2. After reviewing and merging this change, install the following workflow as
   `.github/workflows/sqllens-security-audit.yml` on the private host's default
   branch. Replace `REVIEWED_40_CHARACTER_COMMIT_SHA` with a reviewed commit of
   `The01Geek/sqllens` containing the reusable worker. Do not use a moving ref.
3. Configure the secrets below. No host name or token belongs in a committed file,
   issue, public workflow input, or PR comment.
4. Manually dispatch the public `Weekly Security Audit` workflow on its default
   branch. Verify that the public run contains only dispatch status, that the
   analysis run is in the private host, and that any created advisories remain
   drafts. A complete clean audit creates no advisory.

```yaml
name: SQL Lens Private Security Audit
on:
  workflow_dispatch:
    inputs:
      source_sha:
        required: true
        type: string
      lookback_days:
        required: false
        type: string
        default: '7'
permissions:
  contents: read
jobs:
  audit:
    uses: The01Geek/sqllens/.github/workflows/security-audit-private.yml@REVIEWED_40_CHARACTER_COMMIT_SHA
    with:
      source_sha: ${{ inputs.source_sha }}
      lookback_days: ${{ inputs.lookback_days }}
    secrets:
      AUDIT_CLAUDE_OAUTH_TOKEN: ${{ secrets.AUDIT_CLAUDE_OAUTH_TOKEN }}
      SECURITY_ADVISORY_TOKEN: ${{ secrets.SECURITY_ADVISORY_TOKEN }}
```

| Location | Secret | Purpose and access |
| --- | --- | --- |
| Public SQL Lens repo | `SECURITY_AUDIT_REPOSITORY` | Private host's `owner/repository`; stored as a secret to avoid displaying its name in public logs. |
| Public SQL Lens repo | `SECURITY_AUDIT_DISPATCH_TOKEN` | Fine-grained token scoped to that private host with **Actions: write** and repository metadata access; dispatches its workflow. |
| Private host | `AUDIT_CLAUDE_OAUTH_TOKEN` | Claude Code credential used only by the analysis action. |
| Private host | `SECURITY_ADVISORY_TOKEN` | Token scoped to `The01Geek/sqllens` with **Repository security advisories: write**, owned by an account authorized to manage its advisories. Used only by the deterministic prepare/delivery steps. |

The private host's automatic `GITHUB_TOKEN` needs only `contents: read`; its
metadata access verifies live repository visibility before drafts or reports are
read. The public dispatcher also verifies live private visibility before dispatch.
A denied lookup fails closed. Neither workflow needs `issues: write`.

See GitHub's [repository-advisory API permissions and draft creation contract](https://docs.github.com/en/rest/security-advisories/repository-advisories)
and [workflow-dispatch permissions](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).

## Execution and delivery

The worker refuses public callers. It checks out the current public `main`, with
credentials not persisted, and checks that its HEAD equals the dispatched SHA. If
`main` advanced between dispatch and checkout, rerun the public dispatcher; the
worker fails before analysis. It does not execute a user-supplied branch or ref.
The reusable workflow is pinned by its private caller; the helper and source are
read from trusted public `main`. Review changes to both before activation and
maintain the caller pin when the reusable workflow changes.

Preparation reads the current `.prflow/config.json` model and security-audit effort
(default `medium`), existing draft advisories, and at most 100 recent commits within
the 1–90 day lookback. Draft context is written to a new mode-0600 temporary file,
removed by an always-run cleanup step. Context larger than 256 KB fails before
analysis; reduce or consolidate old drafts before retrying.

The pinned standalone Claude action has Read/Glob/Grep tools only, no shell or
publishing tools, disabled hooks and slash commands, and no repository MCP servers.
The advisory token is absent from its step environment. Model output still requires
maintainer validation; source and draft text are untrusted inputs to the analysis.
No repository code or tests are executed by the analyzer. The workflows configure
no report artifact uploads or caches. Findings and execution diagnostics can remain
in the **private** run logs, so private-host access and retention matter.

Delivery requires a successful action conclusion and a complete, bounded structured
report matching the analyzed SHA. Every finding and existing-draft reference is
validated before the first write. Invalid, oversized, incomplete or truncated
reports fail, and a clean report is distinct from failure. The limit is 20 findings
and 120 KB per report. Repeated audits reuse stable finding markers or explicit
references to existing drafts, including drafts maintained by humans. References
must agree with existing finding markers, and distinct findings cannot claim the
same draft in one report. Matching an unmarked human draft still relies on the
analyzer's judgment and maintainer validation.

Only missing drafts are created. Existing drafts are never edited, closed or
published, so maintainer edits survive reruns. Deduplication covers current drafts;
maintainers must assess recurrence after an advisory has been published or closed.
Affected release ranges, patched versions and CVE assignments are left unset, and
private forks are not requested. Delivery is not transactional: if a later create
fails, the run fails and earlier drafts remain. A rerun can recognize those drafts
by their markers. There is no automatic POST retry or public fallback.

## Verification

`tests/unit/test_private_security_audit.py` exercises dispatch privacy, report
validation, paginated draft lookup, create-only deduplication, partial delivery,
private context files, stale revisions and safe failure diagnostics with mocked
GitHub responses. Run it with `pytest -q tests/unit/test_private_security_audit.py`.
Validate both workflow files with `actionlint` as well.

The Python tests also execute the public commit-selection shell step with passing
and failing git controls, exercise the dispatch guards before network access, and
drive the real HTTP client with synthetic pagination and response-size boundaries.
CI's Python 3.12 job evaluates the public workflow's schedule and manual-dispatch
expressions with GitHub's pinned `@actions/expressions` library. To run those tests
locally, use Node 24 with the Python development environment active (including
PyYAML):

```bash
npm ci --prefix .github/tests/security-audit --ignore-scripts --no-audit --no-fund
npm test --prefix .github/tests/security-audit
```

These checks do not establish live Claude output quality, cross-repository token
permissions, or successful advisory creation. Verify those privately during
activation after the host and credentials are chosen; do not describe the audit as
operational before that private run succeeds.
