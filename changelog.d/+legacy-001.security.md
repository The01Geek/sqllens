Memory listings now return a retryable error when SQL Lens writes overlap the
paged scan, preserving exact totals and ordering without returning partial data.
Coordination covers managed writes within one process; concurrent local writers
from separate processes remain unsupported by Chroma.
