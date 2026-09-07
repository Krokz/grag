# Backup and recovery

## Export and import

The `.lbdb` binary format belongs to the storage engine; the durable escape hatch is JSONL:

```bash
grag --db knowledge.lbdb export -o knowledge.jsonl   # schema + nodes + edges + provenance
grag --db fresh.lbdb import knowledge.jsonl          # replay anywhere (idempotent merge)
```

Embeddings are excluded on purpose — they're derived data and rebuild lazily after import. Commit the export to git for a team-shareable knowledgebase each developer rebuilds locally. Every database is also version-stamped on open (`created_version` / `newest_version` in `_grag_meta`), and grag warns when a database was last written by a newer grag than the one running.

Online export can use `grag export --url http://127.0.0.1:PORT -o knowledge.jsonl`.
It avoids opening a second writer, but does not yet guarantee a single consistent
snapshot during concurrent writes. JSONL excludes internal evidence history and
retry receipts as well as embeddings. Keep the original database if those records
matter; an imported graph is a new retry target. Consistent export and verified
restore remain follow-up work.

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
