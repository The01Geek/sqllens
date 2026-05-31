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

**Warning:** Leave `allow_import` off unless you trust every client that can reach the server. A client able to write memory can influence the SQL that SQL Lens generates for future questions. The command-line `import-memory` and `export-memory` commands are unaffected by this setting and remain the recommended way to manage memory.

## Memory-Administration Tools for the Assistant

For deeper curation than a one-shot import, SQL Lens can expose a set of memory-administration tools to the connected assistant. Set `allow_admin_tools = true` in the `[memory]` section (or `SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS=1`) to enable them. They are off by default. Once enabled, the assistant can list, inspect, add, delete, clear, export and summarize the saved memory through the same connection it uses to answer questions.

The seven tools are:

- **List memories**: returns the saved entries, newest first, with a total count. You can filter to question-and-answer pairs or free-form notes, and limit how many are returned.
- **Get memory**: returns a single entry by its identifier.
- **Delete memory**: removes a single entry by its identifier.
- **Clear memories**: removes all entries, or only one kind, and reports how many were deleted.
- **Add memories**: bulk-adds curated question-and-answer pairs and free-form notes, skipping duplicates automatically. If any entry fails, the tool reports an error to the assistant rather than a success, and lists which entries failed.
- **Export memories**: returns the saved memory as a JSON or CSV blob. The JSON form can be fed straight back into the add tool. If the export would leave anything out, for example free-form notes in a CSV export, the tool reports it as an error so a partial backup is never mistaken for a complete one.
- **Memory statistics**: returns counts of each kind of entry, how often saved patterns were reused in the last 30 days, and the most-reused patterns.

Each entry has a stable identifier you can pass to the get and delete tools. SQL Lens connects to a single database per instance, so although the tools accept a data-source identifier for compatibility, the value does not change which database is used.

**Note:** The tools that change memory (delete, clear and add) refuse to run when the server requires no authentication. To use them, configure authentication (see the [Configuration reference](configuration.md#section-auth)), or set `auth.insecure` to acknowledge that the server runs on a closed, trusted network.

**Warning:** Leave `allow_admin_tools` off unless you trust every client that can reach the server. These tools can read and permanently delete the saved memory, and adding entries can influence the SQL that SQL Lens generates for future questions.

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
