# Managing Memory

SQL Lens keeps a local memory of helpful context so it answers similar questions better over time. You can seed it with curated knowledge up front, and export what it has accumulated, using two command-line commands.

## What Is Stored

SQL Lens remembers two kinds of entries:

- **Question-and-answer pairs**: a natural-language question paired with the SQL that answered it well. A similar future question can reuse that approach instead of working it out from scratch. SQL Lens only saves these automatically when you enable the `save_queries` setting (off by default); you can also seed them from a file at any time with the command-line import.
- **Free-form notes**: short text notes about your schema, for example "in this database, `cust_seg` means customer segment", so future questions land on the right tables and columns.

Both kinds live in the local vector store configured by the `[memory]` section. See the [Configuration reference](configuration.md#section-memory).

## The Bundle File Format

Import and export use a portable bundle file in one of two formats:

- **JSON** (recommended): carries both question-and-answer pairs and free-form notes, and round-trips without losing anything. Use this for a full backup or to move memory between machines.
- **CSV**: a simple two-column spreadsheet with a `question,sql` header. CSV carries question-and-answer pairs only; free-form notes are not included in a CSV export.

A JSON bundle looks like this:

```json
{
  "sql_pairs": [
    { "question": "How many albums did AC/DC release?",
      "sql": "SELECT COUNT(*) FROM albums a JOIN artists r ON a.ArtistId = r.ArtistId WHERE r.Name = 'AC/DC'" }
  ],
  "schema_docs": [
    { "content": "The artists table holds bands and solo performers; albums.ArtistId joins to it." }
  ]
}
```

`sql_pairs` is a flat list of `{question, sql}` objects and `schema_docs` a flat list of `{content}` objects — the same shape the in-app memory widget accepts, so an exported bundle imports back through the command line, the `import_memory` tool, and the widget without reshaping. Both top-level blocks are optional. Each question may be up to 1,000 characters, each SQL statement up to 10,000 characters, and each note up to 50,000 characters. Blank values are rejected.

### Bundle size limits

A complete bundle is also limited in size as a defense against a malformed or hostile file consuming server resources at parse time:

- **10 MiB** (10,485,760 bytes, measured against the raw UTF-8 contents) per bundle file. Files larger than this are rejected with `Invalid memory bundle: bundle exceeds the 10485760-byte cap; split the bundle into smaller files.` before SQL Lens parses them.
- **10,000 items** in each top-level block. A bundle whose `sql_pairs` list or `schema_docs` list exceeds 10,000 entries is rejected with `Invalid memory bundle: bundle '<block>' exceeds the 10000-item cap (got N); split the bundle.`

Realistic curated bundles fit comfortably under both limits. If you have more curated knowledge than fits in one bundle, split it into multiple files and import them sequentially. The limits apply to both the command-line `sqllens import-memory` and the optional `import_memory` MCP tool.

### CSV files and spreadsheet formulas

When SQL Lens writes a CSV bundle (or reads one back in), any cell whose first character is one of `=`, `+`, `-`, `@`, tab, or carriage return is prefixed with a single apostrophe (`'`). Excel and LibreOffice would otherwise interpret these cells as formulas when an operator opens the file in a spreadsheet — a CSV-injection vector (CWE-1236) by which a planted bundle could execute attacker-supplied formulas on the operator's machine. The apostrophe prefix is the documented spreadsheet convention for "treat this cell as text," so files open as expected.

The defang is idempotent — re-importing and re-exporting the same file does not accumulate apostrophes — and is applied only to CSV bundles. JSON bundles are unchanged, because the tools that consume JSON do not interpret leading `=`/`+`/`-`/`@` as formulas. One side effect: a legitimate value whose first character is one of the trigger characters (for example, a SQL fragment starting with `-` from a comment, or a `@`-prefixed identifier) is stored with a leading apostrophe after a CSV round-trip. If you need to preserve such values exactly, use the JSON bundle format.

## Importing Memory

Load a bundle into the configured store:

```bash
sqllens import-memory PATH [--format json|csv] [--clear] [--dry-run] [--batch-size N] [--stream] [-c CONFIG]
```

| Option | Effect |
|---|---|
| `--format` | `json` (default) or `csv`. Must match the file you are importing. |
| `--clear` | Wipe every existing memory in the collection before importing. You are prompted to confirm. |
| `--dry-run` | Validate the file and report what would happen without writing anything. The `--clear` wipe is also skipped. |
| `--batch-size N` | How many entries to write before yielding. The default of `100` is fine for most files; lower it only for very large imports on constrained machines. |
| `--stream` | JSON only. Use a memory-bounded streaming reader that lifts the 10 MiB bundle size limit (per-entry size limits still apply). See [Importing very large bundles](#importing-very-large-bundles) below. |
| `-c CONFIG` | Path to `sqllens.toml`. Falls back to the environment or `./sqllens.toml`. |

Re-importing the same entry is safe: each row is written under a content-derived identifier, so the second import overwrites the existing row in place instead of producing a duplicate. SQL Lens compares entries after trimming whitespace, collapsing internal spaces and lowercasing, so near-identical inputs (different casing, repeated spaces) overwrite the same row. There is no separate "skipped duplicate" count to report.

When the command finishes it prints a summary, for example:

```
saved=42 errors=0
```

A dry run prefixes the summary with `(dry-run)`. If any individual entry could not be saved, the command lists each failure and exits with a non-zero status so it is easy to catch in automation.

**Warning:** `--clear` permanently deletes the current memory before loading the new file. If the import then fails partway through, the collection may be left empty or partial. Take an export first if the existing memory is valuable.

### Importing very large bundles

The default import path holds the whole bundle file in memory at parse time and rejects any file over **10 MiB** or with more than **10,000 entries** in either top-level block. That ceiling is deliberately conservative for the MCP `import_memory` tool, which is exposed to remote clients (see [Letting the Assistant Import Memory](#letting-the-assistant-import-memory)). For an operator working at the command line against a much larger curated bundle, the 10 MiB ceiling is the wrong default.

Pass `--stream` to switch to the memory-bounded streaming reader:

```bash
sqllens import-memory my-large-bundle.json --stream
```

The streaming reader walks the file one record at a time, so memory stays bounded regardless of how large the file is — a 100 MB bundle imports without loading the full document into RAM. The per-entry size limits (1,000 characters for a question, 10,000 for a SQL statement, 50,000 for a free-form note, plus a defensive 1 MiB byte cap per value) still apply row by row. `--stream` is JSON only — a CSV bundle has no streaming benefit because the file is read in a single pass either way.

When the command finishes it prints a summary that also reports how many bytes were read, for example:

```
[stream] saved=120000 failed=0 bytes_read=72831045
```

The streaming path is incremental — it cannot roll back a partial write. If a streaming import is interrupted (a malformed bundle mid-file, a write error, or any per-entry failure), SQL Lens exits with a non-zero status and a clear message; if you combined it with `--clear`, the message warns that the collection may now be empty or partial. SQL Lens also exits non-zero if the file is non-trivial in size but produced zero records (most often a wrong-file or wrong-format mistake) and if `--clear` wiped the store but the import saved nothing — both cases would otherwise leave you with a silently empty collection.

**Tip:** When you plan to use `--stream` together with `--clear`, take an export first (see [Exporting Memory](#exporting-memory)) so you can roll back if the import is interrupted.

## Exporting Memory

Write the configured store to a file:

```bash
sqllens export-memory PATH [--format json|csv] [--stream] [-c CONFIG]
```

Use `--format json` (the default) for a complete, lossless backup. Use `--format csv` only when you want a simple `question,sql` spreadsheet and do not need the free-form notes.

`export-memory` prints a yellow `Warning:` line (and still writes the file) when the export is not a complete picture: the store is empty, some stored rows could not be represented, or `--format csv` dropped schema docs. If the store looks corrupt or was written by an incompatible version, `export-memory` refuses to write a misleading "successful" backup and exits non-zero with no file written — investigate before relying on a backup or running `--clear`.

### Exporting very large stores

Pass `--stream` to write the bundle incrementally without holding the whole store in memory:

```bash
sqllens export-memory my-backup.json --stream
```

The streaming exporter paginates the saved memory and writes each entry to the file as it is read, so memory stays bounded regardless of how many entries are stored. `--stream` is JSON only and writes **atomically**: SQL Lens streams into a sibling temporary file and replaces the destination only after the close-out write succeeds, so a mid-stream disk-full will not destroy a previous backup at the same path.

When the command finishes it prints a summary of the entry counts written, for example:

```
Wrote my-backup.json (sql_pairs=12000, schema_docs=185)
```

The streaming exporter does not refuse a wholesale-corrupt store the way the default path does — the corruption check is a single-snapshot statistic that does not translate cleanly to a paginated read. Instead, every unrepresentable row is surfaced as a `Warning:` line so you still see the corruption signal without a partial export silently succeeding on the rest.

## Letting the Assistant Import Memory

By default, only the command line can import memory. If you set `allow_import = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_IMPORT=1`), SQL Lens additionally exposes an `import_memory` tool to the connected assistant, which accepts a JSON bundle and returns a summary of what was saved.

Re-importing the same logical entry through the tool is idempotent: each row is written under a content-derived identifier, so an existing row is overwritten in place rather than producing a duplicate. If any entry in the bundle fails to save, the tool reports the import as an error to the assistant rather than a success, even when some entries saved and only others failed. A partial import is treated as a failure so the assistant is never told an import succeeded when part of it did not. The reported message gives only the counts of saved and errored entries; the detailed reason for each failure is written to the server log, not returned to the client.

The same 10 MiB / 10,000-item bundle size limits described under [Bundle size limits](#bundle-size-limits) apply to the tool. A bundle that exceeds either cap is refused with `Invalid memory bundle: ...` before any item is written, so a single oversized request cannot block the server. The command-line `--stream` flag (see [Importing very large bundles](#importing-very-large-bundles)) is a command-line-only option for local operators and does not change the limits on this tool.

**Warning:** Leave `allow_import` off unless you trust every client that can reach the server. A client able to write memory can influence the SQL that SQL Lens generates for future questions. The command-line `import-memory` and `export-memory` commands are unaffected by this setting and remain the recommended way to manage memory.

## Memory-Administration Tools for the Assistant

For deeper curation than a one-shot import, SQL Lens can expose a set of memory-administration tools to the connected assistant. Set `allow_admin_tools = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS=1`) to enable them. They are off by default. Once enabled, the assistant can list, inspect, add, delete, clear, export and summarize the saved memory through the same connection it uses to answer questions.

The seven tools are:

- **List memories**: returns the saved entries, newest first, with a total count. You can filter to question-and-answer pairs or free-form notes, and limit how many are returned.
- **Get memory**: returns a single entry by its identifier.
- **Delete memory**: removes a single entry by its identifier.
- **Clear memories**: removes all entries, or only one kind, and reports how many were deleted.
- **Add memories**: bulk-adds curated question-and-answer pairs and free-form notes. Re-adding an existing entry overwrites the stored row in place rather than producing a duplicate. If any entry fails, the tool reports an error to the assistant rather than a success, and lists which entries failed.
- **Export memories**: returns the saved memory as a JSON or CSV blob. The JSON form can be fed straight back into the add tool. If the export would leave anything out, for example free-form notes in a CSV export, the tool reports it as an error so a partial backup is never mistaken for a complete one.
- **Memory statistics**: returns counts of each kind of entry, how often saved patterns were reused in the last 30 days, and the most-reused patterns.

Each entry has a stable identifier you can pass to the get and delete tools. SQL Lens connects to a single database per instance, so although the tools accept a data-source identifier for compatibility, the value does not change which database is used.

**Note:** The tools that change memory (delete, clear and add) refuse to run when the server requires no authentication. To use them, configure authentication (see the [Configuration reference](configuration.md#section-auth)), or set `auth.insecure` to acknowledge that the server runs on a closed, trusted network.

**Warning:** Leave `allow_admin_tools` off unless you trust every client that can reach the server. These tools can read and permanently delete the saved memory, and adding entries can influence the SQL that SQL Lens generates for future questions.

### Interactive Memory-Administration Panel

When `allow_admin_tools` is enabled, SQL Lens also ships a packaged interactive panel that compatible assistants can render inline alongside the chat. The panel groups the seven tools into four sections:

- **Browse**: lists saved memories newest first with a search filter and a row-detail view that exposes a per-row delete action.
- **Import**: accepts a pasted or uploaded JSON bundle and adds it through the same add-memories path as the tool. Re-importing the same entry overwrites the existing row rather than producing a duplicate, and any per-entry failures are surfaced as an explicit error.
- **Stats**: shows count cards for each kind of entry and a top-hits chart for the last 30 days.
- **Danger zone**: exposes export (JSON or CSV), delete one memory by identifier, and clear all or one type. Destructive actions are gated by a type-`CLEAR`-to-confirm box for clear-all and a click-to-confirm dialog for every delete.

The panel runs entirely through your assistant: it makes no direct network connection to the SQL Lens server, so it renders identically whether your assistant reaches SQL Lens directly or through a private proxy. The panel is only advertised when `allow_admin_tools` is enabled, the destructive actions still require authentication (or `auth.insecure = true`) just like the underlying tools, and partial imports or lossy exports are surfaced as an explicit error rather than as a silent success.

**Note:** The interactive panel is supported only by assistants that render MCP App widgets (currently Claude Desktop and claude.ai). Every other assistant continues to use the seven memory-administration tools exactly as before, with no configuration change required on your side.

## See Also

- **[Configuration reference](configuration.md#section-memory)** for every memory setting.
- **[Getting started](getting-started.md)** for a first run against the bundled demo database.
- **[Release notes](release-notes.md)** for what changed in each version.
