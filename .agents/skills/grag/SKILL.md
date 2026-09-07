---
name: grag
description: >-
  Query and build a local graph knowledgebase (grag) for grounded, low-hallucination
  answers. Use when the user asks what the project/codebase knows, wants to "remember"
  a fact or decision, asks how components relate, or wants documents turned into a
  searchable knowledge graph. Also use for RAG grounding: search before answering
  architecture/history/rationale questions about a project that has a .lbdb file.
---

# grag — local-first, LLM-first graph knowledgebase

grag is an embedded Cypher graph DB (LadybugDB) wrapped in an MCP/REST tool contract
designed for LLM grounding. **Local-first**: one `.lbdb` file per project per developer,
zero daemons, nothing leaves the machine. Its core value is **token efficiency** — answer
structural/rationale questions from the graph instead of re-reading source files.

**Core loop:** search/traverse the graph to ground an answer, or build the graph by
defining a schema and upserting nodes/edges. Every fact carries `_source` provenance.

## Always ground in grag first

When a grag database is reachable (an MCP server is configured, `grag serve` is
running, or a `.lbdb` exists you can start), make grag your **first** source of truth
for questions about the project — not a fallback after reading files. This keeps
answers grounded, cuts token spend, and avoids the context bloat that causes
hallucination.

- **Structure/architecture/rationale questions** → `describe_schema`, then
  `search_knowledge` (natural language) or `cypher_query` (exact structural lookups)
  **before** opening source files. The graph usually answers "what/where/how-connected"
  with far fewer tokens than reading code.
- **Read a source file only when** the graph points you at a specific node's
  `path`/`line_start`/`line_end` and you genuinely need the body — not to wander.
- **Build as you learn — check after every substantive exchange, not just when it
  feels obviously important.** `upsert_nodes`/`upsert_edges` it (with `_source`) the
  moment one of these happens, don't wait for a natural pause:
  - The user explains **why** something was built a certain way, rejects an
    alternative, or states a tradeoff → a `Decision` node (the rejected alternative
    and reason belong in its properties, not just the outcome).
  - The user or the code reveals an **external integration** (a service, API,
    library, third-party module and what it's for) → an `Integration`/`Service` node,
    linked to the code that calls it.
  - A **non-obvious concept, pattern, or domain term** gets defined or explained
    (in chat or in a doc/comment) → a `Concept` node — the kind of thing a new
    contributor would otherwise have to ask about or re-derive from source.
  - The user **corrects** something the agent believed about the project → update
    the existing node (don't leave the stale fact standing) or add one if none
    exists yet.
  When you touch an unfamiliar repo, `ingest_code` it first. Prefer writing the
  fact immediately over batching it for "later" — later is where this gets skipped.
- **Reuse labels before inventing them.** `describe_schema` first, then write into
  the table that already covers the concept. `define_schema` refuses a name that
  only differs from an existing one by case, plural or punctuation (`Decisions` vs
  `Decision`) — that refusal means "use the existing table", not "pick a third name".
- **Always pass `source`.** Every upsert takes a `source` (recorded as `_source`,
  with `_created_at` added automatically): the file, ticket, PR or session the fact
  came from. That is what lets a later session tell a decision from a guess.
- **Connect words to code.** Link knowledge nodes to the code they describe (e.g. a
  `Decision`/`Concept` `-[:DOCUMENTS|MENTIONS]->` a `Function`/`Module`), so retrieval
  returns docs *and* the implementation in one cited subgraph.

**Check code freshness before relying on recent edits.** Serving reads trigger
background verification of registered source contents and Git HEAD when present;
plain folders also work. Saved paths, `calls`, `max_file_kb`, and `incremental`
options survive refreshes and restarts. You usually do not need to re-ingest after
each edit, but a later read is not guaranteed to be fresh: failures stay visible
and retry with backoff on reads; an idle server does not poll.

All four graph-read MCP tools accept `freshness` and `freshness_timeout_ms`:

- `allow_stale` (default) reads the current graph without waiting for verification.
- `wait` waits up to the deadline, then permits unverified evidence with
  `freshness.timed_out: true`.
- `require` rejects the read unless verification succeeds. Use it when an answer
  depends on current code. The default deadline is 5000 ms; the range is 0–60000.

Inspect `freshness.status` in the response/footer. Only `fresh` verifies the
registered code scope at `checked_at`; `checking`, `refreshing`, `stale`, `error`,
`unknown`, and `disabled` do not. This does not certify authored memories,
embeddings, or source edits made after the check. A timeout bounds the verification
wait, not graph-query execution. Inspect `GET /api/index/status` for root errors,
saved options, generations, and retry delays. Legacy indexes report `unknown`
until an explicit `ingest_code` with the intended paths/options enrolls them;
never guess that a file-only scope meant its entire parent directory. Missing or
moved paths require diagnosis and reconciliation, not deletion of the database.
Direct Python services must call `enable_auto_refresh()` to enable verification;
`require` fails if checking is disabled.

If grag is genuinely unavailable (no server, no DB), say so and proceed without it —
don't stall.

> **Semantic search is opt-in.** `search_knowledge` defaults to BM25 full-text only.
> For significantly better recall on natural-language queries, enable vector search:
> ```
> pip install 'gragdb[embed-local]'          # fastembed + ONNX, ~50-100 MB, no API key
> GRAG_EMBED_PROVIDER=fastembed grag --db <file> serve --with-mcp
> ```
> If the user hasn't enabled this, suggest it. When `search_knowledge` returns
> `pending_embeddings > 0`, nodes are still being embedded — the server's background
> worker drains the backlog on its own within seconds. A footer with no `pending_embeddings`
> field does **not** mean "fully embedded" — check the `vector` field instead:
> absent/missing means vector search ran fine, `"vector":"off"` means no
> embedder is configured on this server process (FTS-only is expected, and
> `pending_embeddings` will never appear), `"vector":"error"` means an embedder
> is configured but failed for that call — report it rather than guessing.

## How to talk to it

**Use the checkout's saved database.** Local `grag init` records its selection in
Git-ignored `.grag/project.json`. CLI commands discover it from subdirectories;
new worktrees/checkouts get separate databases even when folder names match.
Explicit `--db`/`--db-dir` wins over environment selectors, which win over the
mapping. Without a mapping, unambiguous project Claude/Cursor registrations are
respected, then the resolved project root's `knowledge.lbdb` is the fallback.
Python `GragConfig` still takes an explicit path/environment selection.
To share memory intentionally, use `grag --db /path/to/shared.lbdb init` in each
checkout and connect to one server. Do not copy the mapping between worktrees.

**After moving a checkout**, stop its server and disconnect auto-starting clients,
then use `grag relocate /old/root /new/root --dry-run` to inspect the changes and
the same command without `--dry-run` to apply. Older installations can select the
existing database with global `--db`. Relocation changes roots, provenance paths,
saved code scope, and local Claude/Cursor launch configuration while retaining
graph IDs, memory text, and relationships. It never moves or creates a database.
If no database is present, only a matching checkout mapping can be reconciled;
the missing database is reported. A copied folder uses `init` for a new identity.

Graph updates are transactional; client files use init's checked writes/backups.
If publication fails after graph commit, rerun the same relocation to finish
configuration. Restart clients afterward and verify code freshness. Legacy code
indexes still need explicit indexing options if none were recorded. User-scope
registrations need `init --client <client>`; arbitrary installation discovery and
linked skill repair are separate. Existing documents remain available, but later
document re-ingestion can produce new IDs after a move; document synchronization
does not yet preserve their identity across relocation.

Pick the first surface that is available, in this order:

1. **MCP tools** — if the `grag` MCP server is configured, the 10 tools appear directly.
2. **REST** — if `grag serve` is running (default `http://127.0.0.1:8471`):
   `POST /api/{query,search,context,ingest,ingest/code}`, `GET /api/{schema,graph/sample,index/status,health}`,
   `POST /api/{schema/define,nodes/upsert,edges/upsert}`.
3. **Python** — `from grag.service import GragService` with `GragConfig(db_path=...)`.
   Methods mirror the tools exactly.

If none are running and a `.lbdb` exists, start one:
`GRAG_EMBED_PROVIDER=fastembed grag --db <file> serve --with-mcp` (UI + REST + MCP, with semantic search)
or `grag --db <file> serve` (UI + REST only, FTS-only search).

## If grag stops responding

A connection or readiness error does not establish the cause. If the MCP proxy
reports a dead upstream, wait a few seconds and retry the tool once. If the server
was deliberately stopped, reconnect MCP or use `grag --db <file> start` to resume.

For repeated failure, use the exact database path in the client MCP registration:

1. Run `grag --db <file> status` and inspect the reported daemon log. Confirm the
   database, process, and port before restarting anything.
2. Act on the actual error:
   - A lock error means another process owns the database. Use that server or
     stop the correct owner before opening the file elsewhere.
   - A missing path after a folder move calls for checking the launch command,
     indexed checkout roots, and database location. Do not recreate or delete
     an unfamiliar database merely because the working directory changed.
   - A WAL/shadow replay error requires offline recovery. Stop clients/servers
     and pause any supervisor that would reopen this database. Run
     `grag --db <file> recover`. This preserves the database and its sidecars
     with checksums, attempts strict replay on a copy, and prints a manifest
     plus the verified copy's path. The original remains in place.
   - If strict replay fails, `recover --allow-data-loss` permits partial replay
     on a fresh copy. Committed writes may be lost; the amount is unknown.
     Use this option only when the user has authorized potentially lossy
     recovery. Review important memories before adopting the recovered path.
     Keep the preserved files even if recovery fails. Never delete a WAL or
     shadow file as a repair step: committed data may depend on it.
   - A port serving another database calls for correcting this client's target
     or choosing an unused port; do not stop an unrelated server.
3. When the database opens normally, `grag reindex` can rebuild embeddings.
   It cannot repair a database that fails to open. Ordinary starts never
   perform lossy recovery, including with legacy `GRAG_WAL_AUTO_RECOVER=1`.
4. If the server is healthy but the MCP session is broken, REST can provide
   access while the user reconnects MCP. For unresolved failures, report the
   exact error and attempted steps; do not silently retry-loop.

## The 10 tools

| tool | use |
|---|---|
| `describe_schema` | **Call first**, before writing any Cypher. Returns tables, properties, row counts, sample keys as prompt-shaped text. Prevents hallucinated labels. |
| `define_schema` | Create node/rel tables. Design the graph for the domain — but reuse first: near-duplicate names (case/plural/punctuation of an existing table) are refused with the existing name in the hint; `allow_similar=true` only for a genuinely different concept. |
| `upsert_nodes` / `upsert_edges` | Atomic MERGE batches; `upsert_nodes` can include `edges`. Optional retry IDs and revision guards; provenance is automatic. |
| `cypher_query` | Read-only Cypher. Write keywords (CREATE/MERGE/SET/DELETE/...) are rejected — use the upsert tools for writes. |
| `search_knowledge` | Hybrid BM25 + vector seeds, RRF fusion, **per-label diversity cap**, k-hop expansion, token-budgeted cited context. The main RAG entry point. |
| `get_context` | Re-pack chosen node ids; page a long STRING with `text_property`. |
| `ingest_code` | Index a repo's code STRUCTURE (Python, TS/JS/Vue, C#, Terraform, Go, Bash, Java, Kotlin, Rust, C/C++, Ruby, PHP, Swift, Lua, Scala, SQL): Repo/Module/Class/Function/TerraformModuleCall nodes + CONTAINS_*/IMPORTS/INHERITS/CALLS edges. Structure only — never source bodies. |
| `ingest_docs` | Index Markdown/text files on the server as a graph: `Document -> Section` from the heading hierarchy (`SUBSECTION_OF`, `NEXT_SECTION`), body chunks `IN_SECTION`, and `MENTIONS_FUNCTION/CLASS/MODULE` edges for backtick-mentioned code symbols. Run `ingest_code` first so those resolve. Use for specs and design docs. |
| `job_status` | Poll a background ingest (`ingest_code` / `ingest_docs` with `background=true`) by id — use background mode for large trees so the call returns immediately. |

**Save related nodes and edges in one `upsert_nodes(nodes, edges=...)` call.**
Both upsert tools are atomic; an error saves none of that call's graph changes.
Edge endpoints can be existing nodes or nodes included in the combined call.
Schema definition stays separate. Always check `warnings`: undeclared, reserved,
or mismatched properties are skipped; invalid keys or unknown request fields are
errors. Put primary keys in `key`, never `properties`. `null` clears a property.

For a write whose response might be lost, supply a unique `operation_id` (1-128
characters). Retry the exact same payload and ID. A committed retry returns its
original result with `replayed=true` and does not undo later edits. A changed
payload with the same ID conflicts; a new intended edit needs a new ID. Receipts
survive restarts in this database file and do not expire automatically. JSONL
export/import does not preserve receipts; treat an imported graph as a new retry
target. Operation IDs are optional for ordinary writes.

Before a competing edit, use `cypher_query` to return the whole node (`RETURN n`)
or relationship (`RETURN a,r,b`), then pass its `_revision` as `expected_revision`
on the upsert item. Use `expected_revision="absent"` for create-only writes.
All guards check the state before the batch writes; any conflict rejects it all.
REST returns 409 and a conflict code; MCP includes `CODE: revision_conflict` or
`CODE: operation_id_conflict`. Read current evidence and reconcile before retrying.
Guarded/retryable writes include a `revisions` map; replayed revisions describe
the original commit. These are content/provenance tokens, not history counters;
identical content can produce the same token again. `_revision` is computed in
whole-entity query results, not a stored Cypher column. Search output stays compact.

Re-ingesting documents replaces their generated links atomically. Relationships
you create or update with `upsert_edges` remain authored and are preserved.
Check ingestion `warnings`: obsolete sections/chunks with remaining authored or
unknown links are retained and may describe an earlier revision. Legacy links
without ownership metadata are preserved for explicit review; do not assume
re-ingestion removed them. New ingests track ownership automatically.

## Read complete evidence

`search_knowledge` and `get_context` both return a JSON footer after `---` in MCP.
Check `truncated`, `omitted_nodes`, `omitted_edges`, `omitted_properties`, and
`expansion_limited` before treating the context as complete. Packing retains
whole values; a missing property may simply have exceeded the budget. The
structured graph and seeds contain only packed records/properties. Expand the
budget, reduce hops, or retrieve a specific value before drawing a conclusion
that depends on missing evidence. Search still covers only its requested seeds,
hops, and candidate limits, even when `truncated` is false.

For a long STRING, call `get_context` with exactly one canonical node id,
`text_property="body"` (or the actual STRING property from `describe_schema`),
and `text_offset=0`. Page mode skips graph expansion. Read the exact property
slice and the `text_page` footer; continue with `text_offset=next_offset` and
`text_sha256=sha256` until `next_offset` is null. If the text changed, discard
earlier slices and restart at offset 0 without a hash. The last page may still
say `truncated=true` because it is only a suffix of the value.

`token_budget` is at least 256 (default 2000). It bounds the larger of the
complete compact JSON response and the MCP context plus footer, using
`ceil(UTF-8 bytes / 4)` as an estimate, not a model-specific tokenizer count.
`response_token_estimate` measures that payload; `token_estimate` measures only
the context text. Protocol envelopes and client-added formatting are excluded.

## Session memory: what to record, and how to pick it up next time

grag ships no fixed schema — the graph is yours to design — but session knowledge
converges when every session speaks the same vocabulary. Unless the project already
uses other names (check `describe_schema`), use these labels, each with `title`,
`text`/`body`, `status` where it applies, and always a `source`:

| label | what goes in it | typical links |
|---|---|---|
| `Task` | work the user asked for or you took on; `status` open/done/blocked; the acceptance criterion | `CONCERNS_MODULE`/`CONCERNS_FUNCTION` → code it touches |
| `Decision` | a choice made and **why**, incl. the rejected alternative | `DOCUMENTS` → the code that embodies it; `SUPERSEDES` → an older Decision |
| `Insight` | a non-obvious fact you established from code or the user (a gotcha, an invariant, a perf number) | `ABOUT_FUNCTION`/`ABOUT_MODULE` |
| `Question` | something unresolved that needs the user or more digging; `status` open/answered | `ANSWERED_BY` → Decision/Insight |
| `Concept` / `Integration` | domain terms, patterns, external services (see above) | `MENTIONS_*` → code |

One rel table per (from, to) pair — `CONCERNS_FUNCTION` and `CONCERNS_MODULE` are
two tables, not one with two targets.

**Session start:** `describe_schema`, then `search_knowledge` for open work
(`labels=["Task","Question"]`, query like "open") and for the area you are about to
touch — you inherit what earlier sessions learned instead of rediscovering it.
**During the session:** write Tasks when work starts, Decisions/Insights as they
happen, Questions when you are blocked; update `status` rather than adding
duplicates. **Session end:** mark finished Tasks done, leave open Questions open.

## Working with an unfamiliar codebase (code graph)

`ingest_code` is the token-frugal way to explore a repo. The loop:

1. **Index first**: call `ingest_code` on the repo path(s) BEFORE reading files.
   Re-running is incremental (only changed files are rewritten) and prunes removed
   files/symbols/edges; a serving grag refreshes registered paths after source edits.
   Folder moves require explicit relocation as described above.
2. **Ask structural questions with `cypher_query`** over the code tables:
   - what imports X: `MATCH (m:Module)-[:IMPORTS]->(x:Module) WHERE x.path = '<path>' RETURN m.id`
   - what calls Y: `MATCH (f:Function)-[:CALLS]->(y:Function) WHERE y.path = '<path>' AND y.name = '<name>' RETURN f.id`
   - subclass/implementor check: `MATCH (c:Class)-[:INHERITS]->(b:Class) WHERE b.name = 'Base' RETURN c.id`
   - what version is a Terraform module pinned to: `MATCH (m:TerraformModuleCall) WHERE m.source CONTAINS '<name>' RETURN m.version, m.source`
   - ids look like `Module:pkg-<path-hash>:core.py` and
     `Function:pkg-<path-hash>:core.py#Greeter.greet`;
     nodes carry signature, docstring and line range — enough to navigate by.
3. **Only fetch source bodies when truly needed** — read the file at the node's
   `path`/`line_start`/`line_end` instead of bulk-reading the repo.

Python parses in every install (stdlib ast); every other language needs
`pip install "gragdb[code]"` (tree-sitter + tree-sitter-language-pack) — without it
those files raise an ERROR with that install HINT. Supported: TypeScript/JavaScript
(and the `<script>` of `.vue`), C#, Terraform, Go, Bash, Java, Kotlin, Rust, C, C++,
Ruby, PHP, Swift, Lua, Scala, SQL (tables → `Class`, views/functions/procedures →
`Function`). CALLS/INHERITS edges are Python-only for now; IMPORTS elsewhere is
best-effort (path/package-based). Rust `impl` and Swift `extension` methods attach
to the type when it is declared in the same file, else stand as `Type.method`.
Go has no lexical class nesting — methods are top-level funcs with a receiver,
attached to their struct/interface's Class node by receiver type name, and
interface method sets become Function nodes too. Go IMPORTS resolution is
narrower than the others: it matches an import's declared package name
against locally scanned packages (no go.mod parsing, so no true import-path
prefix), which resolves single-file local packages and silently skips
everything else (stdlib, third-party, ambiguous multi-file local packages).

**Never hand-type a fact that `ingest_code` already extracts structurally.**
Terraform `module` blocks (local or registry/git source alike) become
`TerraformModuleCall` nodes with `name`/`source`/`version` read straight off
the `.tf` file — a version pin queried this way can't drift from the actual
source the way a value copied from a README or changelog can. If you're
about to `upsert_nodes` a version/pin/path fact that a code file already
states verbatim, `ingest_code` the repo and `cypher_query` it instead.

## Hard-won rules (respect these — they came from real errors)

- **Always `describe_schema` before `cypher_query`.** Confirm labels/properties exist.
- **Primary key value goes in `key`, never also in `properties`.** LadybugDB rejects
  `SET` on a primary-key column: "Cannot set property ... used as primary key."
- **One rel table per name.** A rel name maps to exactly one `FROM`-label/`TO`-label
  pair. To link several target types, create separate tables (e.g. `MENTIONS_MODULE`,
  `MENTIONS_CONCEPT`) — you cannot redefine an existing rel name with new endpoints.
- **Rel endpoints must already exist as node tables.** Define/ingest the node tables
  first, then define rels that reference them. (Note: `Chunk` is created by `ingest`,
  not by `define_schema`.)
- **`cypher_query` is read-only.** Use `upsert_nodes`/`upsert_edges` for writes.
- **Near-duplicate labels are refused.** `define_schema` with `Decisions` when
  `Decision` exists returns an ERROR naming the existing table — reuse it.
- **Properties starting with `_` are grag-internal** (provenance, vector codes). The
  mutation layer rejects caller writes to them.
- **Node ids are `Label:key`** (e.g. `Module:core.engine`). Use `split_node_id` /
  `make_node_id` conventions when correlating ids with primary keys.
- **Errors are self-describing:** tool output is `ERROR: ... HINT: ...`. Read the hint
  and retry — it's written for you.

## Operational gotchas

- **Single-writer:** the embedded engine holds a write lock per `.lbdb`, so `serve`
  and `mcp` cannot share one file as separate processes ("Could not set lock"). For a
  live UI watching MCP writes, run ONE process: `grag --db <file> serve --with-mcp`
  mounts the MCP endpoint (`/mcp`) on the REST/UI server — UI + REST + MCP share one
  registry and one write conn, so the UI sees writes as they land. Otherwise use
  separate files.
- **Shutdown:** new work is refused and queued jobs become `cancelled`; active
  ingests, verification, requests and embedding drain before the database closes.
  The server gives them a 10-second grace period, then reports any pending drain
  and retains the engine until that work exits. A stuck Python/native call can
  keep the process alive. If stop reports that it has not exited, inspect the
  daemon log and retained registration; a timeout does not mean it stopped.
  Resubmit cancelled jobs after restart. Job records are process-local.
- **FTS index is built lazily** on first search of a table (a write-path cost). Warm it
  with one search before timing latency.
- **Extensions are preloaded at engine startup.** A table with an FTS or HNSW index
  rejects reads/writes/index-maintenance while its extension is unloaded in-process
  ("Trying to insert into an index ... but its extension is not loaded"). `Engine.__init__`
  LOADs FTS + VECTOR once (tolerant when offline), so no path needs per-call handling.
  Extensions are scoped to the Database, so the write conn's LOAD covers pooled readers.
- **Buffer pool:** creating FTS indexes needs headroom; tests use a 128MB pool.
- **Vectors:** default codec `fp32` (native HNSW); `int8`/`binary`/`polar` are opt-in via
  `GRAG_VECTOR_CODEC`. Without an embedder, everything works FTS-only.
- **Search is diversity-capped per label** (`GRAG_SEARCH_LABEL_CAP`, default 2). A big
  code table can't flood out knowledge tables on a general "why/what" question — a
  `Decision`/`Concept` still surfaces even when hundreds of `Function` nodes match the
  tokens. To aim at one table anyway, pass `labels=[...]`. Set the cap to 0 for pure
  rank order.

## Multiple databases

**Related repos belong in ONE `.lbdb`:** `ingest_code` with several paths puts each repo
as a separate `Repo` node in a single graph, so you can trace CALLS/IMPORTS across repo
boundaries or link a Decision in one project to a Function in another. Separate `.lbdb`
files are for UNRELATED projects (isolation). Choose per project set, then be consistent.

A server started with `--db-dir` hosts many `.lbdb` files; one file = one isolated
universe. Detect it with `GET /api/dbs` — `dbs` non-empty means multi-db.

- **Target a DB** with `?db=<name>` on REST calls, or the `x-grag-db: <name>` header
  (REST and HTTP MCP). Query param wins over header.
- **Pick the DB for the current project and use it consistently.** Don't mix entities
  across DBs — there are no cross-db queries.
- **Single-writer still applies per file:** only ONE process can write a `.lbdb`.
  When several IDE windows need the same DB, use the shared HTTP MCP server
  (`grag --db-dir <dir> mcp --transport streamable-http`, header per window) instead
  of per-window stdio processes, which collide with "Could not set lock".

## Enabling semantic (vector) search

Hybrid search is off until you give it an embedder. The supported path is **fastembed**
(ONNX Runtime — *no* PyTorch), which keeps grag light:

```bash
pip install -e ".[embed-local]"            # fastembed + onnxruntime, ~50-100MB
GRAG_EMBED_PROVIDER=fastembed grag --db knowledge.lbdb serve
```

- Model defaults to `BAAI/bge-small-en-v1.5` (384-dim); override with
  `GRAG_EMBED_MODEL` / `GRAG_EMBED_DIM`. Downloads once to a cache (~50MB), then offline.
- Steady state is ~300ms/query on CPU. Server RSS rises from ~120MB to ~530MB.
- A serving grag embeds new/updated nodes on a background worker (ingests return at
  once; `pending_embeddings` in the search footer shrinks on its own). The embedding
  text is the node's prose STRING props — `meta`, `path`, `heading_path`, `language`
  and git fields are left out — and queries/documents get the model family's retrieval
  prefixes automatically. Model, prefix, text-policy, endpoint, and codec changes
  make old vectors pending for automatic rebuilding; legacy vectors rebuild once.
  `grag reindex` forces a rebuild (also use it when a model changes behind the same
  name/endpoint). A dimension mismatch requires explicit storage migration.
- Alternative: `GRAG_EMBED_PROVIDER=remote` + `GRAG_EMBED_BASE_URL` (+`GRAG_EMBED_API_KEY_ENV`)
  for any OpenAI-compatible endpoint — but that sends data off-box. Not the default.
- Do **not** reach for PyTorch/sentence-transformers (2.5GB, against the lightness
  budget) or a custom-trained embedder (no training data; engineering vanity).

## Building a knowledge graph from documents

1. `define_schema` with node tables per entity type and rel tables per relationship.
2. `ingest` the documents (markdown/JSON/CSV) — creates `Chunk` nodes with provenance.
3. `upsert_nodes` for entities, `upsert_edges` for relationships.
4. Link chunks to entities with MENTIONS-style rels so retrieval returns cited subgraphs.
5. Verify with `search_knowledge` and a few `cypher_query` traversals.

See `examples/build_self.py` in this repo for a complete working example (grag
describing grag).
