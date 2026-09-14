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

## Ordinary JSON schemas, contracts and fixtures

**New in 0.10.0:** select document mode explicitly:

```text
ingest_docs(paths=["/absolute/project/contracts"], json_mode="document")
```

```bash
grag ingest --json-mode document contracts/schema.json fixtures/example.json
```

Each `.json` file becomes one source document, including arrays, empty objects or
arrays, and scalar values. JSON syntax is checked before ingestion. Its original
text is indexed with the local file as provenance; values are not reserialized,
rounded or turned into graph properties. UTF-8 and UTF-8 with a BOM are supported.
The ordinary chunker splits longer text, so an individual chunk is partial source
content and may not be valid JSON by itself. Open the cited file for full structure
or exact field locations; JSON Pointers and field-level line citations are not
generated. With `sections=true`, JSON text gets one preamble section, rather than
a graph of JSON properties.

Chunk `meta` includes `format="json"`, `coverage="source_text"` and
`source_sha256`, the SHA-256 of the input file bytes. This identifies the source
version ingested; it does not verify that the file is still current. Grag checks
JSON syntax, not JSON Schema conformance, and does not resolve `$ref`, apply
defaults, validate fixture values against a schema, or fetch external files/URLs.
Duplicate keys and numeric spelling remain visible as source text.

The mode applies to **all `.json` files selected by that call**, even a file whose
shape resembles a document-record import. Use separate calls for collections
with different intended formats. Markdown/text behavior is unchanged, and
`.jsonl` remains a document-record format. Ignore rules, symlink exclusions and
nested repository/worktree boundaries still apply to explicit files as well as
directories.

JSON document nesting is limited to 64 levels. Existing source limits still
apply: 64 selected paths, 256 files/documents and 2 MiB of loaded bytes per call,
plus request and generated-mutation limits. Invalid syntax/encoding or excessive
depth produces a skipped-file warning and disables deletion synchronization for
the incomplete scan. Byte/batch resource-limit failures abort before ingestion;
split the selected inputs and retry. There is no silent truncation to make a
file fit. For background ingestion, inspect initial loader warnings as well as
the completed `job_status` result.

## JSON document-record imports

The default `json_mode="records"` preserves the existing format:

```json
[
  {"text": "Exports stay local.", "source": "design:exports", "metadata": {"team": "platform"}}
]
```

A `{"documents": [...]}` wrapper is also accepted. JSONL contains one such
record per non-empty line. Each record keeps its own text, optional provenance
and metadata. These records are not ordinary JSON source files: a schema object
or unrelated fixture shape is skipped with warnings in this mode. An empty
record array synchronizes to no documents; an empty array in document mode
indexes the literal `[]`. Successful re-ingestion, including an explicit mode
switch, reconciles the selected file's prior loader-owned content.

Python callers can use
`load_request(paths, json_mode="document", sections=True)` from
`grag.ingest.loaders`, then pass the returned request to `GragService.ingest`
or submit its JSON through the existing REST `/api/ingest` endpoint. Check the
loader's warnings and file count. Raw `IngestRequest(documents=[...])` still takes
already loaded documents; it does not read JSON paths or auto-detect formats.

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
