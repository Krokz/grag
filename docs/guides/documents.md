# Index documents

For a running shared server, ask the agent to call:

```text
ingest_docs(paths=["/absolute/project/docs"], sections=true, background=true)
```

Paths are on the server's filesystem. Poll the returned job with `job_status`.
The CLI also routes through the selected graph owner:

```bash
grag ingest --sections docs/
```

Section-aware Markdown ingestion builds `Document → Section → Chunk` relationships.
Code-symbol mentions can link into an existing code graph. Read the returned counts
and warnings to confirm the intended files were processed. Batches are limited to
256 documents and 2 MiB of loaded content; split larger collections.

## Re-ingestion and retained evidence

 Re-ingesting a document
replaces its generated section, reading-order, chunk, and code-mention links in
one transaction. Relationships created or updated through `upsert_edges` remain
authored, including when their source is that document. Obsolete sections/chunks
with remaining authored or unknown relationships are retained with a warning;
their content may describe an earlier revision. Legacy relationships without
ownership metadata are also preserved with a warning for explicit review.
New ingests track ownership automatically, with no additional setup or flags.
Check ingestion `warnings` (or the background job result); the CLI prints them.

## Synchronize a source collection

Document ingestion uses the same ignore and boundary policy as
[code indexing](code.md). Re-ingesting a directory synchronizes successfully scanned
sources, including deleted files and shrinking JSON/JSONL batches. It also reconciles
switches between sections/flat mode and chunk labels. A failed or unreadable scan
preserves omitted sources and reports that deletion synchronization was skipped.
Documents are not automatically refreshed; rerun ingestion after edits.

Only loader-owned data is reconciled. Authored and legacy unknown relationships
retain their obsolete endpoints. Stable source identities survive `grag relocate`
and subsequent re-ingestion. Raw Python/REST ingestion can supply `sync_paths` and
`IngestDocument.source_file` for an authoritative file collection; ordinary
`documents=[...]` calls synchronize only their named sources.
