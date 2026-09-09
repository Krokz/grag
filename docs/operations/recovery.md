# Backup and recovery

## Export and restore

Use the same two commands for a local database or one already owned by a grag server:

```bash
grag --db knowledge.lbdb export -o knowledge.jsonl
grag --db restored.lbdb import knowledge.jsonl
```

`export` automatically uses the registered owner. For an explicit server, use
`grag export --url https://host -o knowledge.jsonl`; `--server-db NAME` selects a
database on a multi-db server and `GRAG_API_TOKEN` supplies authentication.
A direct stdio session that owns the file must close first or use the shared server
setup. Older servers without snapshot format 2 must be restarted with updated grag.

Export captures one committed graph under the writer lock, including schema,
provenance, source ownership, lifecycle state, saved ingestion options, evidence
history and retry receipts. Writes wait during capture; delivery reads a completed
temporary file and releases the database lock. Other graph reads remain available.
The snapshot represents database state at capture, not a copy or verification of
source files. A write committed afterward belongs to the live database only.

Format 2 ends with a completion record containing a SHA-256 checksum and record
counts/digests per table. The CLI validates the full stream before publishing a
backup, even when writing to stdout. File output is staged beside the requested
file and atomically replaces it only on success. A failed download leaves any
previous backup intact. An abrupt process exit can leave `.NAME.partial-*` files;
those are staging files, not completed backups. Checksums detect incomplete or
changed bytes; they do not authenticate the author of a backup.

`import` requires a **new local destination**. It validates the archive before
creating a database, restores schema and records in one transaction in a separate
`.grag-restore-*` staging directory, checkpoints, closes, strictly reopens, and
compares the full graph, history and receipts. Only then does it atomically publish
the destination without overwriting an existing file or sidecar. Failure cleans up
staging; abrupt termination may leave its directory for inspection. Publication
requires a filesystem supporting hard links (including normal local NTFS/APFS/ext4).
There is no in-place restore or merge into a populated graph. The lower-level
Python importer treats replay of the identical archive as a no-op, preserving edits
made since that restore.

Node content revisions, history sequence numbers and `r2:` relationship revisions
survive equivalent logical restore. Relationship guards describe content and are
scoped by the upsert's type and endpoint keys, independent of native storage IDs.
Legacy unprefixed relationship guards require a reread before a new edit; they are
never silently treated as current tokens. Old receipts still return their original
revision strings, so reread current state before editing after a replay. A retry whose
receipt is in the snapshot returns its original result without undoing later edits;
reuse with a changed payload still conflicts. Receipts and edits committed after
the snapshot are absent: restoring does not promise continuity beyond that point.
Restore creates no new authored review events and does not free receipt/history
capacity. Historical relationship topology was never recorded.

Embeddings, indexes and runtime version stamps are excluded. Search rebuilds
indexes and, with the same optional embedding configuration, vectors. The first
search may therefore take longer or report pending embeddings. Paths and saved
source scope are retained; use `freshness=require` to verify code after restore,
and the explicit [relocation workflow](../guides/projects.md) if source paths moved.
Source files, client configuration, credentials and background-job state need their
own backup. Restoring never repoints a running server or changes editor settings;
review the copy, then deliberately select it with `--db` or `grag init`.

Externally created tables without grag provenance are supported for STRING, INT64,
DOUBLE, BOOL, DATE and TIMESTAMP columns, including typed primary keys and duplicate
relationships. Unknown internal schemas, other native types, custom column defaults,
non-finite floats and multi-endpoint relationship tables fail explicitly instead of
being silently omitted. Individual JSONL records are capped at 16 MiB. Capture and
restore need temporary disk space proportional to the snapshot/database; they are
bulk operations, not ordinary tool-response-sized requests.

### Older exports

Version 1 exports cannot prove completeness and omitted history, retry receipts and
internal ownership/lifecycle state. Explicitly accept these limits with:

```bash
grag --db legacy-copy.lbdb import old.jsonl --allow-legacy
```

This verifies the restored contents against the records present but cannot prove
that the old export finished. The command warns about missing continuity; treat it
as a new retry target and reconcile source ownership by re-ingesting intended scopes.
Only string primary keys can be restored from v1 because that format omitted their
types. Keep the original database if its omitted state matters.

## Recover a database that cannot open

Stop every process using the database and pause any supervisor that would restart
it. Run `grag --db /path/to/knowledge.lbdb recover`. The command first locks and
copies the database, WAL, and existing shadow/checkpoint sidecars into a
recovery bundle with checksums (0700 directories and 0600 files on POSIX).
Native replay runs on a separate copy in a child
process. The original files remain in place; no recovery attempt opens them for writes.

If strict replay fails, inspect the error and preserved files. To explicitly allow
partial WAL replay, rerun with `--allow-data-loss` (and a new `--out-dir`, if supplied).
The result may omit committed writes, including authored memories that code
re-ingestion cannot reconstruct. The manifest records whether partial replay was
used and which table counts survived; the exact loss is unknown. Never delete the
WAL or shadow file to fix an error.

A successful run prints the recovered database path. Verification checks checkpoint,
strict reopen, table counts, and sample property reads; it does not prove semantic
completeness. Review important memories against source records or a known-good
backup before pointing the server/client at that path. Keep the bundle, including
failed attempts. `grag reindex` can rebuild embeddings once a database opens; it
cannot fix a failed open. The deprecated `GRAG_WAL_AUTO_RECOVER=1` no longer enables
in-place lossy recovery, including in supervised deployments.
