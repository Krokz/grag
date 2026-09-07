# Resource limits

Normal use needs no additional configuration. Requests are
limited to 2 MiB, upserts to 1,000 total nodes plus edges, and searches/context
lookups to 64 seeds/IDs and 64 labels. Search shares a pool of at most 1,024
candidates per modality across labels; narrowing labels gives each more room.
Neighborhood expansion shares 1,024 paths across seeds (at most 512 per seed)
and reports `expansion_limited` when clipped. Exact cosine still scores the
eligible vectors; hitting a work limit returns an error instead of silently
sampling vectors or presenting partial ranking as exact.

Read operations share limits of 64 MiB of decoded-result JSON, 100,000 result
rows and 4,096 statements. Lexical processing shares 4 MiB of text and 200,000
terms. Packing shares 128 MiB of rendered text work and 8,192 build attempts.
Text pages hash the selected property in 64K-character projections and load
only the requested window and its citation/lifecycle fields. They still verify
the whole value each call, within the shared read limits; stored history
snapshots are at most 1 MiB; new history identities and entry metadata are capped
at 2 KiB and 16 KiB so they remain readable within a supported page budget. Ordinary JSON/MCP responses are capped at 1 MiB;
explicit streaming JSONL export is separate. These are application bounds,
not a process-memory ceiling: native query execution and a single decoded value
can allocate before Python checks them. The native buffer pool and statement
timeout remain in force.

Each database admits at most 32 active operations and 16 running/queued jobs.
A full queue returns `resource_limit`; poll existing jobs before resubmitting.
Finished job history is bounded to 200 entries. Admission reserves room for
completion/error details; full job records are byte-bounded. Oversized results
become readable failed jobs, and oversized error messages/names are explicitly
marked as truncated. Source scans share 256 MiB and
100,000 directory/file entries across roots and verification passes. An incomplete
scan never certifies freshness. Document batches accept at most 256 documents,
with 2 MiB of loaded file content; narrow paths or split batches when needed.

Authored history keeps at most 1,000 entries per node, 100,000 overall and a
conservative 256 MiB storage allowance. Retry receipts keep at most 100,000
entries and a conservative 64 MiB allowance. Existing strings are accounted at
four bytes per Unicode character. Capacity failures roll back the entire upsert;
history and receipts are never silently evicted, and existing operation IDs
remain replayable at capacity. Preserve the database and continue in a new graph
if durable storage fills. Supersession chains allow 128 links and 1,024 traversed
links per mutation batch. `resource_limit` includes the limiting resource and a
correction hint; REST uses 413 for size/work limits and 429 for busy admission,
and MCP marks the tool result as an error.
