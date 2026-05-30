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

Realistic curated bundles fit comfortably under both limits. If you have more curated knowledge than fits in one bundle, split it into multiple files and import them sequentially. The limits apply to both the command-line `sqllens import-memory` and the optional `import_memory` MCP tool.

### CSV files and spreadsheet formulas

When SQL Lens writes a CSV bundle (or reads one back in), any cell whose first character is one of `=`, `+`, `-`, `@`, tab, or carriage return is prefixed with a single apostrophe (`'`). Excel and LibreOffice would otherwise interpret these cells as formulas when an operator opens the file in a spreadsheet — a CSV-injection vector (CWE-1236) by which a planted bundle could execute attacker-supplied formulas on the operator's machine. The apostrophe prefix is the documented spreadsheet convention for "treat this cell as text," so files open as expected.

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

`export-memory` prints a yellow `Warning:` line (and still writes the file) when the export is not a complete picture: the store is empty, some stored rows could not be represented, or `--format csv` dropped schema docs. If the store looks corrupt or was written by an incompatible version, `export-memory` refuses to write a misleading "successful" backup and exits non-zero with no file written — investigate before relying on a backup or running `--clear`.

## Letting the Assistant Import Memory

By default, only the command line can import memory. If you set `allow_import = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_IMPORT=1`), SQL Lens additionally exposes an `import_memory` tool to the connected assistant, which accepts a JSON bundle and returns a summary of what was saved.

If any entry in the bundle fails to save, the tool reports the import as an error to the assistant rather than a success, even when some entries saved and only others failed. A partial import is treated as a failure so the assistant is never told an import succeeded when part of it did not. The reported message gives only the counts of saved, skipped and errored entries; the detailed reason for each failure is written to the server log, not returned to the client.

The same 10 MiB / 10,000-item bundle size limits described under [Bundle size limits](#bundle-size-limits) apply to the tool. A bundle that exceeds either cap is refused with `Invalid memory bundle: ...` before any item is written, so a single oversized request cannot block the server.

**Warning:** Leave `allow_import` off unless you trust every client that can reach the server. A client able to write memory can influence the SQL that SQL Lens generates for future questions. The command-line `import-memory` and `export-memory` commands are unaffected by this setting and remain the recommended way to manage memory.

## See Also

- **[Configuration reference](configuration.md#section-memory)** for every memory setting.
- **[Getting started](getting-started.md)** for a first run against the bundled demo database.
- **[Release notes](release-notes.md)** for what changed in each version.
