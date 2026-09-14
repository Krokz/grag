---
name: grag
description: Query and maintain a local grag knowledge graph for project grounding, decisions and agent memory. Use when grag is configured or a project .lbdb exists, when invoked as grag in a new checkout, or when asked to build or query a graph knowledgebase.
---

# grag

Use the project's graph to answer questions about its structure, decisions and
history, and save useful findings with sources for the next session. Storage and
retrieval are local; the agent harness or an explicitly remote embedder may send
context to its provider. Follow the user's chosen scope and database.

## Prefer MCP for graph work

Prefer configured grag MCP tools for reads, writes and ingestion in the selected
graph. Discover deferred tools before declaring them unavailable. Use `upsert_nodes`
for memory, `search_knowledge` for search, and `ingest_code` / `ingest_docs` for indexing.
Do not substitute equivalent CLI commands or Python/HTTP scripts while MCP is available.

Use the CLI for setup/server management, diagnostics, backup/recovery, explicit user
requests, or unavailable/failed MCP connections. Briefly state the fallback reason
and keep the same database/server. Validation errors and empty results do not warrant
switching. Return to MCP when available, including after CLI setup.

## First invocation in a checkout

On a bare `/grag` (or the harness's equivalent skill invocation), proactively
initialize and index the current project's source if its graph is absent, empty,
or contains only grag's verified init marker. Follow the
[first-use procedure](references/operations.md).
Existing code, documents or authored memory means use that graph as it stands;
do not automatically widen or rebuild it. Missing tools or a connection error
are not proof of an empty graph. A specific user task or source scope takes
precedence; merely loading this skill during other work does not request a full scan.

## Choose the smallest useful read

1. Confirm the MCP connection addresses the intended graph. For CLI fallback,
   `.grag/project.json` selects the checkout's database; explicit `--db` overrides it.
2. Read `describe_schema` before Cypher or new writes. Reuse known labels and
   properties. Default compact schema is enough for most work; `detail="full"`
   adds counts/samples. Reuse `schema_revision` with `if_revision` for the same
   detail level instead of repeatedly loading an unchanged schema.
3. For an exact name, count, status or relationship, use a projected
   `cypher_query`. For a fuzzy question, use `search_knowledge`, narrowing `labels`
   when known. Use `get_context` only for needed neighbors, selected evidence,
   history or long-text paging; these are choices, not a mandatory tool chain.
4. Read source bodies at returned file/line citations when the graph is
   insufficient. Index an unfamiliar unindexed source with `ingest_code` when
   structural navigation will help; avoid re-ingesting a known fresh scope.

Example after checking the schema:

```cypher
MATCH (f:Function) WHERE f.name = 'refresh'
RETURN f.id, f.path, f.line_start, f.signature
```

Whole entities (`RETURN n`, `RETURN r`) include computed `_revision` for guarded
edits and omit derived vectors/null columns in MCP. `_revision` is not a stored
Cypher property. Explicit projections remain exact, including nulls and vectors.
Return endpoints with relationships for canonical graph IDs; native `_ID`, `_SRC`
and `_DST` are not persistent identities.

## Check what the result establishes

- Graph reads default to `freshness="allow_stale"`. Use `require` when current
  source matters; `wait` permits unverified evidence at its deadline. Only
  `freshness.status="fresh"` verifies the registered code scope at `checked_at`.
  It does not certify memories, embeddings or later edits. The timeout bounds
  verification, not query execution. Legacy unknown scopes need explicit enrollment.
- Search/context default to `evidence="current"`: explicitly obsolete,
  superseded, retracted, expired and disputed evidence is excluded. Use `all`
  for qualified old evidence. Unreviewed evidence remains eligible, not certified.
  Cypher is unfiltered; an empty edge result is not proof of complete parser coverage.
- Inspect `truncated`, omission counters and `expansion_limited`. `text_excerpts`
  are partial; follow up before treating them as a full policy or procedure.
  Search is limited by requested labels, candidates and hops even without truncation.
- For a long STRING, use one ID and `get_context(text_property=...)`; continue
  with `text_offset=next_offset` and `text_sha256=sha256` until null. If changed,
  discard old slices and restart at zero. A final suffix may still be truncated.
- `token_budget` is a UTF-8/4 estimate (256–32768, default 2000), not a model
  tokenizer count. Narrow results or page text instead of repeatedly enlarging them.

## Save useful context

For memory corrections, retirement, "continue", "resume" or "what next", load
[memory](references/memory.md).
Confirm the checkout/database, then use exact status and established priority/scope
fields or a current priority decision. Search scores, task IDs and mission numbers
are not priority. Fetch the chosen task's acceptance, next step and linked current
decisions/questions within a budget. Missing scope or priority means uncertainty;
`evidence="current"` does not itself mean a task is unfinished or a question unanswered.

Reuse the schema and existing records. Save decisions and their reasons, corrections,
open work and non-obvious findings with `source`; connect them to relevant code
where an appropriate relationship exists. Prefer ingested code facts over copied
versions/paths that can drift. Mark completed work done rather than duplicating it.
Replace the current summary when state changes; use evidence history for prior text,
not an ever-growing chronological body. Keep completion and release status distinct.

`upsert_nodes` can include related `edges` in one atomic call. Put the primary key
in `key`, never `properties`; omitted fields preserve values, null clears them.
Check warnings for skipped properties. Validation/statement failures roll back;
an uncertain completion needs the original operation ID and stored-state check
(see operations). Stay within
1000 total nodes/edges and 2 MiB per call.

For competing edits, read the whole entity and pass its `_revision` as
`expected_revision`; use `"absent"` for create-only. Relationship revisions use
`r2:` and survive logical restore, scoped by the selected type/endpoints. Legacy
unprefixed relationship guards require a reread. Node revision format is unchanged.
For a possibly lost response, retry the exact payload and `operation_id`. A receipt
replays the original result without undoing later edits; its revisions describe that
old commit. A changed payload needs a new ID. Reconcile conflicts before a new edit.

Add `evidence: {}` to start correction history; changing evidence on an existing
node requires its revision. Before corrections, reviews, expiry, supersession or
history reads, load [references/memory.md](references/memory.md).

## Load detail only when needed

- [references/ingestion.md](references/ingestion.md): code/document enrollment,
  scope changes, exclusions, retained obsolete nodes and parser coverage.
- [references/operations.md](references/operations.md): setup, multiple harnesses,
  moved folders, backups/recovery, connection failures or optional embeddings.

BM25 works by default; vector search is optional. `vector="off"` is expected
without an embedder; `error` means configured embeddings failed. Do not enable
models solely because vectors are off. A serving database has one owner: use the
shared server for multiple clients. Never delete WAL/shadow files as a repair or
history/receipts to bypass capacity limits. If unavailable, report the actual
failure and continue useful work without inventing graph evidence.
