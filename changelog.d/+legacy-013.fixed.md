Memory administration bounds add batches to 10,000 items per block and
list results to 0–1000 rows, scanning storage in pages while retaining
newest-first ordering and the full matching count. Saved-query searches
accept limits from 1–100. Per-item validation and storage failures keep
their indices without returning raw validation inputs or exception details.
(#221, #223)
