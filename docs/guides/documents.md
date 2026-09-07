# Index documents

For a running shared server, ask the agent to call:

```text
ingest_docs(paths=["/absolute/project/docs"], sections=true, background=true)
```

Paths are on the server's filesystem. Poll the returned job with `job_status`.
For offline CLI use, with no server or client holding the selected database:

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

## Current scope limits

Document directories are not automatically synchronized like registered code
scopes. Re-ingest deliberately after edits. Deletions, shrinking batches with the
same source, changing chunk labels/modes, and source moves need care: full document
sync and stable document identities across moves are unfinished. Keep the original
graph when testing a new scope and inspect generated and authored evidence.
