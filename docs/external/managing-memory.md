# Managing Memory

SQL Lens keeps a local memory of helpful context so it answers similar questions better over time. You can seed it with curated knowledge up front, export what it has accumulated, and verify that the assistant still produces the SQL you expect for a curated set of reference questions, using three command-line commands.

## What Is Stored

SQL Lens remembers two kinds of entries:

- **Question-and-answer pairs**: a natural-language question paired with the SQL that answered it well. A similar future question can reuse that approach instead of working it out from scratch. SQL Lens only saves these automatically when you enable the `save_queries` setting (off by default); you can also seed them from a file at any time with the command-line import.
- **Free-form notes**: short text notes about your schema, for example "in this database, `cust_seg` means customer segment", so future questions land on the right tables and columns. SQL Lens only writes these itself when you enable the `save_text_memories` setting (off by default); notes you add with the command-line import or the memory-administration tools are stored whatever that setting is, and are never affected by it.

Both kinds live in the local vector store configured by the `[memory]` section. See the [Configuration reference](configuration.md#section-memory).

## Near-Match Background Context

By default, SQL Lens reaches into its memory in two ways: the assistant can search saved question-and-answer pairs on demand, and SQL Lens automatically adds any closely matching free-form notes to the assistant's background context before it answers. Both use the `similarity_threshold` setting as the bar a memory must clear. The assistant's on-demand search uses its own wording of the question, so it can miss a saved answer that matches the original question closely.

You can optionally turn on a wider automatic background context. Set `context_similarity_threshold` **below** `similarity_threshold` in the `[memory]` section. SQL Lens then searches memory with the user's exact question and adds, as related context, up to five saved question-and-answer pairs and up to five free-form notes that score at or above `context_similarity_threshold` — including very close matches. A pair is shown to the assistant as a related example ("a similar earlier question was answered with this SQL"), not as an answer to copy. Very long SQL is shortened.

For example, to keep the strict bar at the default `0.7` but also surface anything scoring `0.55` or higher as related context:

```toml
[memory]
similarity_threshold = 0.7
context_similarity_threshold = 0.55
```

`context_similarity_threshold` is not set by default, which keeps this tier off. Lower it gradually: the lower the value, the more loosely related memories SQL Lens includes, which can help on questions that never quite match but also adds length and noise to what the assistant reads. During normal serving, question-and-answer pairs added this way count as retrieved in the memory statistics.

If a request uses a named profile with its own `similarity_threshold`, that value is used as the strict bar for this tier too.

If SQL Lens cannot reach its memory while assembling this context, it quietly proceeds without the extra context rather than failing the question.

## Settling Ambiguous Questions

When a question can be read more than one way and the readings would count different records, a different period, or a different measure, the assistant is instructed to ask one short clarifying question instead of answering. Memory lets you steer which of those choices it asks about. These are instructions to the assistant, not enforced rules, so it may occasionally ask or answer when it should not.

- **Mark a known ambiguity** with a free-form note that states, as a fact, that a term has more than one meaning. For example: "Revenue has two meanings here: gross revenue includes shipping and tax, net revenue excludes them." A question about revenue that the note reaches is then meant to get a clarifying question unless something settles which meaning is meant: the question itself, an earlier turn of the conversation, or one of the two kinds of memory below.
- **Stop the question with a note** that names the meaning to use. For example: "When someone asks about revenue, use net revenue: exclude shipping and tax."
- **Stop the question with a saved question-and-answer pair** for that question. For example, the question "What was revenue last month?" saved with SQL that sums order totals minus shipping and tax. When the assistant's memory search finds the pair, it is instructed to use the reading that the pair's SQL uses.

A note reaches the assistant only on a turn whose message the automatic background context retrieves it for (see [Near-Match Background Context](#near-match-background-context)). A note that does not match the current message closely enough neither marks nor settles the choice on that turn. The assistant's memory search uses its own wording of the question, so it can miss a saved pair that would have settled the choice and ask anyway.

## The Bundle File Format

Import and export use a portable bundle file in one of two formats:

- **JSON** (recommended): carries both question-and-answer pairs and free-form notes, and round-trips without losing anything. Use this for a full backup or to move memory between machines.
- **CSV**: a simple two-column spreadsheet with a `question,sql` header. CSV carries question-and-answer pairs only; free-form notes are not included in a CSV export.

A JSON bundle looks like this:

```json
{
  "sql_pairs": {
    "training_type": "sql_pairs",
    "pairs": [
      { "question": "How many albums did AC/DC release?",
        "sql": "SELECT COUNT(*) FROM albums a JOIN artists r ON a.ArtistId = r.ArtistId WHERE r.Name = 'AC/DC'" }
    ]
  },
  "schema_docs": [
    { "training_type": "schema_docs",
      "content": "The artists table holds bands and solo performers; albums.ArtistId joins to it." }
  ]
}
```

Both top-level blocks are optional. Each question may be up to 1,000 characters, each SQL statement up to 10,000 characters, and each note up to 50,000 characters. Blank values are rejected.

### Bundle size limits

A complete bundle is also limited in size as a defense against a malformed or hostile file consuming server resources at parse time:

- **10 MiB** (10,485,760 bytes, measured against the raw UTF-8 contents) per bundle file. Files larger than this are rejected with `Invalid memory bundle: bundle exceeds the 10485760-byte cap; split the bundle into smaller files.` before SQL Lens parses them.
- **10,000 items** in each top-level block. A bundle whose `sql_pairs.pairs` list or `schema_docs` list exceeds 10,000 entries is rejected with `Invalid memory bundle: bundle '<block>' exceeds the 10000-item cap (got N); split the bundle.`
- **64 levels** of nested arrays and objects in a JSON bundle, counting the outermost object as level 1. A valid bundle nests only a few levels deep, so this limit only affects malformed files; a deeper file is rejected with `Invalid memory bundle: bundle JSON is too deeply nested to parse safely` on every supported Python version.

Realistic curated bundles fit comfortably under these limits. If you have more curated knowledge than fits in one bundle, split it into multiple files and import them sequentially. The limits apply to both the command-line `sqllens import-memory` and the optional `import_memory` MCP tool.

### CSV files and spreadsheet formulas

When SQL Lens writes a CSV bundle (or reads one back in), any nonblank cell whose first character is one of `=`, `+`, `-`, `@`, tab, carriage return, or newline is prefixed with a single apostrophe (`'`). Excel and LibreOffice would otherwise interpret these cells as formulas when an operator opens the file in a spreadsheet — a CSV-injection vector (CWE-1236) by which a planted bundle could execute attacker-supplied formulas on the operator's machine. The apostrophe prefix is the documented spreadsheet convention for "treat this cell as text," so files open as expected.

The query-results widget applies the same prefix rule to CSV headers and cells. When a prefix is added, its download, clipboard and manual-copy outcomes warn that those values will be treated as text in spreadsheets, including negative numbers. CSV delimiter quoting also preserves embedded commas, quotes, carriage returns and newlines.

The defang is idempotent — re-importing and re-exporting the same file does not accumulate apostrophes — and is applied only to CSV bundles. JSON bundles are unchanged, because the tools that consume JSON do not interpret leading `=`/`+`/`-`/`@` as formulas. One side effect: a legitimate value whose first character is one of the trigger characters (for example, a SQL fragment starting with `-` from a comment, or a `@`-prefixed identifier) is stored with a leading apostrophe after a CSV round-trip. If you need to preserve such values exactly, use the JSON bundle format.

## Importing Memory

Load a bundle into the configured store:

```bash
sqllens import-memory PATH [--format json|csv] [--clear] [--dry-run] [--batch-size N] [-c CONFIG]
```

| Option | Effect |
|---|---|
| `--format` | `json` (default) or `csv`. Must match the file you are importing. |
| `--clear` | Wipe every existing memory in the collection before importing. You are prompted to confirm. |
| `--dry-run` | Validate the file and report what would happen without writing anything. The `--clear` wipe is also skipped. |
| `--batch-size N` | How many entries to write before yielding. The default of `100` is fine for most files; lower it only for very large imports on constrained machines. |
| `-c CONFIG` | Path to `sqllens.toml`. Falls back to the environment or `./sqllens.toml`. |

Duplicate entries are skipped automatically. An entry counts as a duplicate when an identical one is already stored or appears earlier in the same file, comparing after trimming whitespace and ignoring letter case. Re-importing the same file is therefore safe and saves nothing the second time.

When the command finishes it prints a summary, for example:

```
saved=42 skipped_duplicate=3 errors=0
```

A dry run prefixes the summary with `(dry-run)`. If any individual entry could not be saved, the command lists each failure and exits with a non-zero status so it is easy to catch in automation.

**Warning:** `--clear` permanently deletes the current memory before loading the new file. If the import then fails partway through, the collection may be left empty or partial. Take an export first if the existing memory is valuable.

## Exporting Memory

Write the configured store to a file:

```bash
sqllens export-memory PATH [--format json|csv] [-c CONFIG]
```

Use `--format json` (the default) for a complete, lossless backup. Use `--format csv` only when you want a simple `question,sql` spreadsheet and do not need the free-form notes.

`export-memory` prints a yellow `Warning:` line (and still writes the file) when the export is not a complete picture: the store is empty, some stored rows could not be represented, or `--format csv` dropped schema docs. CSV formula-prefix additions also produce a nonfatal warning because the exported text changed; use JSON to preserve exact values. If the store looks corrupt or was written by an incompatible version, `export-memory` refuses to write a misleading "successful" backup and exits non-zero with no file written — investigate before relying on a backup or running `--clear`.

## Verifying Memory After an Import

`import-memory` writes curated question-and-answer pairs straight into the memory the assistant uses at query time, with no safety net of its own. `verify-memory` is the post-import regression guard: it runs a curated **golden file** of reference questions through the live assistant and reports, per question, whether SQL Lens still produces the SQL you expected.

Each reference question is a full live run of the assistant using the persisted `default` profile's retrieval threshold and limits, capped by the configured row and iteration limits. SQL details are forced on for comparison. It typically makes several billed requests to your language-model provider and runs the SQL the assistant generates against your configured database. Size your golden file with that cost in mind.

`verify-memory` does not save memories or update retrieval counters. For the duration of the run, the assistant's tools for saving question-and-answer pairs and notes are turned off, even if your configuration enables saving queries. The assistant still reads memory as usual, which is what you are testing.

```bash
sqllens verify-memory PATH [--format json|csv] [--fail-under PCT] [-c CONFIG]
```

| Option | Effect |
|---|---|
| `--format` | `json` (default) or `csv`. The golden file uses the same shape as a memory bundle's question-and-answer pairs (the `sql_pairs` block in JSON, or a `question,sql` CSV), so any tool that can write a bundle can write a golden file. |
| `--fail-under PCT` | Pass-rate percentage tolerance from 0 to 100. See the exit-code rules below; at least one case must PASS. |
| `-c CONFIG` | Path to `sqllens.toml`. Falls back to the environment or `./sqllens.toml`. |

A golden file is structurally a memory bundle's question-and-answer block — the same 10 MiB, 10,000-item and JSON nesting limits apply, and the same blank-value and length rules are enforced. CSV golden values are preserved exactly; unlike memory import/export, verification does not add spreadsheet formula-protection prefixes. The free-form notes block, if present, is ignored: only question-and-answer pairs are reference cases.

A typical workflow is to import an updated bundle, then immediately verify it:

```bash
sqllens import-memory new-bundle.json
sqllens verify-memory golden.json
```

For each case, `verify-memory` classifies the assistant's answer as one of:

- **PASS**: The assistant's SQL, after normalization, matches the expected SQL.
- **CHANGED**: Both the expected and actual SQL parsed cleanly, but the normalized forms differ. The assistant's SQL changed and needs review. CHANGED does not always mean the answer is wrong — two correct queries with different JOIN order, or `IN` versus `EXISTS`, both register as CHANGED — so the per-case detail shows both queries side by side for inspection.
- **ERROR**: Either the expected or actual SQL could not be parsed, or the assistant failed to produce a SQL statement (the language model errored, an internal tool failed, and so on).

When the command finishes it prints a table of results and a summary line, for example:

```
PASS 7 | CHANGED 2 | ERROR 1 (total 10, pass-rate 70.0%)
```

Per-case details follow the summary for any non-PASS result, showing the expected SQL, the captured actual SQL (or `(not available)`), and an error message (when the run failed). Parse-failure diagnostics identify whether the golden SQL or the assistant's SQL could not be parsed. A missing SQL statement has its own diagnostic.

Normalization uses the configured database dialect. Quoted and unquoted identifiers can still compare differently; the comparison does not prove semantic equivalence.

A golden question that leaves a choice open, such as "What was revenue last month?" when revenue has two meanings and nothing settles which one, may get a clarifying question instead of SQL (see [Settling Ambiguous Questions](#settling-ambiguous-questions)). When no query ran before the question, the case is reported as ERROR. When the assistant ran a lookup query first, for example to find which customers match a name, that lookup's SQL is the actual SQL compared. State the choice in your golden questions ("What was net revenue, excluding shipping and tax, last month?") so they test the SQL rather than the question.

**Exit codes** follow SQL Lens's strict "never silent success" rule:

- An **empty golden file** (no question-and-answer pairs) exits non-zero with an explicit error. There is nothing to verify, so a clean exit would be misleading.
- **Any non-PASS case** exits non-zero by default. Use `--fail-under` to relax this when occasional variability is expected.
- An **all-non-PASS run** always exits non-zero, even with `--fail-under 0`, so a completely broken assistant cannot pass an unrestricted threshold.
- With **`--fail-under PCT`** and at least one PASS, the command exits zero when the PASS percentage is at or above PCT and non-zero otherwise.

**Note:** The assistant's answers are not perfectly deterministic. A single CHANGED result on a re-run is not necessarily a real regression — re-running may produce a PASS. If you expect some noise in normal operation, set `--fail-under` to the pass rate you require (for example, `--fail-under 90` to pass when at least 90 percent of cases PASS).

## Letting the Assistant Import Memory

By default, only the command line can import memory. If you set `allow_import = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_IMPORT=1`), SQL Lens additionally exposes an `import_memory` tool to the connected assistant, which accepts a JSON bundle and returns a summary of what was saved.

Calls also require `auth.mode = "bearer"`, or an explicit `auth.insecure = true` acknowledgement on a closed network. With the default `auth.mode = "none"` and `auth.insecure = false`, the tool returns an error before parsing or importing the bundle. This applies to both stdio and HTTP connections.

If any entry in the bundle fails to save, the tool reports the import as an error to the assistant rather than a success, even when some entries saved and only others failed. A partial import is treated as a failure so the assistant is never told an import succeeded when part of it did not. The reported message gives only the counts of saved, skipped and errored entries; the detailed reason for each failure is written to the server log, not returned to the client.

The same 10 MiB, 10,000-item and 64-level limits described under [Bundle size limits](#bundle-size-limits) apply to the tool. A bundle that exceeds any of them is refused with `Invalid memory bundle: ...` before any item is written, so a single oversized request cannot block the server.

**Warning:** Leave `allow_import` off unless you trust every client that can reach the server. A client able to write memory can influence the SQL that SQL Lens generates for future questions. The command-line `import-memory` and `export-memory` commands are unaffected by this setting and remain the recommended way to manage memory.

## Memory-Administration Tools for the Assistant

For deeper curation than a one-shot import, SQL Lens can expose a set of memory-administration tools to the connected assistant. Set `allow_admin_tools = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS=1`) to enable them. They are off by default. Once enabled, the assistant can list, inspect, add, delete, clear, export and summarize the saved memory through the same connection it uses to answer questions.

The seven tools are:

- **List memories**: returns the saved entries, newest first, with a total count. You can filter to question-and-answer pairs or free-form notes, and limit how many are returned.
- **Get memory**: returns a single entry by its identifier.
- **Delete memory**: removes a single entry by its identifier.
- **Clear memories**: removes all entries, or only one kind, and reports how many were deleted.
- **Add memories**: bulk-adds curated question-and-answer pairs and free-form notes, skipping duplicates automatically. If any entry fails, the tool reports an error to the assistant rather than a success, and lists which entries failed.
- **Export memories**: returns the saved memory as a JSON or CSV blob. The JSON form can be fed straight back into the add tool. If the export would leave anything out, for example free-form notes in a CSV export, the tool reports it as an error so a partial backup is never mistaken for a complete one. CSV formula-prefix additions produce a nonfatal warning; they do not set the dropped-record `lossy` flag.
- **Memory statistics**: returns counts of each kind of entry, how often saved patterns were reused in the last 30 days, and the most-reused patterns.

Each entry has a stable identifier you can pass to the get and delete tools. SQL Lens connects to a single database per instance, so although the tools accept a data-source identifier for compatibility, the value does not change which database is used.

**Note:** The tools that change memory (delete, clear and add) refuse to run when the server requires no authentication. To use them, configure authentication (see the [Configuration reference](configuration.md#section-auth)), or set `auth.insecure` to acknowledge that the server runs on a closed, trusted network.

**Warning:** Leave `allow_admin_tools` off unless you trust every client that can reach the server. These tools can read and permanently delete the saved memory, and adding entries can influence the SQL that SQL Lens generates for future questions.

If memory changes during a listing, the tool returns a retry error instead of
partial rows or an unreliable total. Retry after the write finishes. This covers
SQL Lens writes within one running process, including background agent writes.
Do not run concurrent writers from separate processes against the same local
memory directory; Chroma does not support that deployment.

### Interactive Memory-Administration Panel

When `allow_admin_tools` is enabled, SQL Lens also ships a packaged interactive panel that compatible assistants can render inline alongside the chat. The panel groups the seven tools into four sections:

- **Browse**: lists saved memories newest first with a search filter and a row-detail view that exposes a per-row delete action.
- **Import**: accepts a pasted or uploaded JSON bundle and adds it through the same add-memories path as the tool, with duplicates skipped automatically and any failures surfaced as an explicit error.
- **Stats**: shows count cards for each kind of entry and a top-hits chart for the last 30 days.
- **Danger zone**: exposes export (JSON or CSV), delete one memory by identifier, and clear all or one type. Destructive actions are gated by a type-`CLEAR`-to-confirm box for clear-all and a click-to-confirm dialog for every delete.

The panel runs entirely through your assistant: it makes no direct network connection to the SQL Lens server, so it renders identically whether your assistant reaches SQL Lens directly or through a private proxy. The panel is only advertised when `allow_admin_tools` is enabled, the destructive actions still require authentication (or `auth.insecure = true`) just like the underlying tools, and partial imports or lossy exports are surfaced as an explicit error rather than as a silent success.

**Note:** The interactive panel is supported only by assistants that render MCP App widgets (currently Claude Desktop and claude.ai). Every other assistant continues to use the seven memory-administration tools exactly as before, with no configuration change required on your side.

## See Also

- **[Configuration reference](configuration.md#section-memory)** for every memory setting.
- **[Getting started](getting-started.md)** for a first run against the bundled demo database.
- **[Release notes](release-notes.md)** for what changed in each version.
