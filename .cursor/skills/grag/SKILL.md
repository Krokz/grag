---
name: grag
description: Query and maintain a local grag knowledge graph for project grounding, decisions and agent memory. Use when grag is configured or a project .lbdb exists, when invoked as grag in a new checkout, or when asked to build or query a graph knowledgebase.
---

# grag

Use the project's graph to answer questions about its structure, decisions and
history, and save useful findings with sources for the next session. Storage and
retrieval are local; the agent harness or an explicitly remote embedder may send
context to its provider. Follow the chosen scope/database.

## Prefer MCP for graph work

Prefer configured grag MCP tools for graph reads, writes and ingestion. Discover deferred tools before declaring them unavailable.
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

1. Confirm the intended graph; `.grag/project.json` selects it for CLI fallback,
   and explicit `--db` overrides it.
2. Use source search for straightforward code navigation. Use grag for saved
   decisions/history and structural relationships such as callers or cross-file
   impact. A graph lookup is useful when it supplies evidence the task needs.
3. For graph topic search, use a focused `search_knowledge` query with known
   labels; try `top_k=4, hops=0` for an initial lead. No schema preflight is needed.
   A repository name in query text is not a scope filter.
4. Use projected `cypher_query` for graph counts/status and exact relationships
   or IDs. Check unfamiliar schema; reuse known labels/properties. Compact schema
   suffices; full adds counts/samples. Reuse `schema_revision` with `if_revision`
   at the same detail level.
5. Stop recall when the relevant memory supplies the requested claim, scope,
   source and qualifications. Read again for a specific gap: missing text or
   neighbors, conflicting evidence, current-code verification or an edit guard.
   Off-topic hits call for source inspection or corrected scope. Empty results
   do not prove absence.

After checking schema (filter by known source root in shared graphs):

```cypher
MATCH (f:Function) WHERE f.name = 'refresh'
RETURN f.id, f._source, f.line_start, f.line_end
```

Whole entities include computed `_revision` for guarded edits and omit vectors/nulls
in MCP. `_revision` is not a stored property. Explicit projections remain exact.
Return endpoints with relationships for canonical IDs; native `_ID`, `_SRC` and
`_DST` are not persistent identities.

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

## Reuse investigations across sessions

Before finishing a useful investigation or handoff, preserve explicit user decisions,
reasons and reusable findings with scope, sources, limits and next steps. Cite the
discussion for choices and source inspection for observations; keep unchosen proposals
distinct. Reuse existing records/fields; skip routine reads and ingested facts.
Load [memory](references/memory.md) for capture, correction, retirement or resumption.

For prior findings, use a focused lookup or `get_context` for a known ID. Reuse
sufficient evidence for recall; verify relevant current source for implementation
claims. Within an authorized memory workflow, correct verified stale findings with
revision guards and history without asking again. Preserve the user's decision when
implementation diverges. Respect read-only scope; report unresolved discrepancies.

For resume, use the selected task or exact unfinished status and the project's
priority/scope convention; scores and mission numbers are not priority. Fetch its
acceptance, next step and linked decisions/questions. Current evidence can include
done tasks. Missing scope or priority remains uncertain; see the memory reference.

Link relevant code/tasks where relationships exist. Keep summaries current and
completion distinct from release status; preserve prior text in history.

`upsert_nodes` accepts related `edges` atomically. Put primary keys in `key`, never
`properties`; omission preserves values, null clears. Check skipped-property warnings.
Limits: 1000 total nodes/edges and 2 MiB. For uncertain completion, use the original
operation ID and check stored state (operations reference).

Read a whole entity before editing and pass its `_revision` as `expected_revision`;
use `"absent"` for create-only. Add `evidence: {}` to start correction history.
Retry a lost response with the exact payload and `operation_id`; read again before
another edit. Conflict, review, lifecycle and history procedures are in the memory
reference; never treat a retry receipt as the latest revision.

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
