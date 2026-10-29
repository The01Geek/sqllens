# Dependency resolution and audit

`requirements.txt` records the core runtime closure with Python/platform markers, generated from the existing major-version bounds in `pyproject.toml`. Regenerate it as a whole using the command in its header. CI installs with this file as constraints for its Python 3.11/3.12 and database connector jobs, then runs `pip check` and the normal tests. Optional database drivers and developer tools are outside the core lockfile.

The separate Dependency audit workflow runs pinned `pip-audit` on the recorded core closure for Linux/Python 3.11 and 3.12 on PRs, main pushes, and manual dispatch. Markers for other platforms are not audited by these jobs. The audit is advisory: findings or scanner failures produce a warning and retain their step output; they do not block a merge. An unavailable scan is not a clean result. No issues are opened automatically. This does not change the weekly source-code security audit's reporting destination.

Four explicit exclusions apply only to the current embedded Chroma integration:

| Advisory | Reason for exclusion |
|---|---|
| [PYSEC-2026-311 / GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c) | Chroma HTTP collection/model endpoint; SQL Lens uses embedded `PersistentClient` and `DefaultEmbeddingFunction`. |
| [PYSEC-2026-3813 / GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) | Chroma server authorization; SQL Lens does not expose Chroma HTTP routes. |
| [PYSEC-2026-3814 / GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf) | Chroma HTTP collection/model endpoint; SQL Lens does not accept a model repository or remote-code option for its embedded store. |
| [PYSEC-2026-3815 / GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) | Chroma server RBAC; SQL Lens does not run that server or authorization provider. |

Evidence: `src/sqllens/memory/store.py` and `src/sqllens/agent/integrations/chromadb/agent_memory.py` construct embedded clients. Revisit every exclusion when a patched Chroma release is available, when an advisory changes scope, or before introducing remote Chroma servers, custom embedding repositories, or Chroma authorization providers. Other advisories remain visible. These exclusions do not claim that hostile local store files are safe.
