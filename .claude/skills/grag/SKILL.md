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

**The code graph stays fresh on its own.** A serving grag fingerprints every indexed
checkout (HEAD plus dirty/untracked files) and re-ingests it incrementally in the
background when something moved, so you do **not** re-run `ingest_code` after each
edit or commit. When a `search_knowledge` footer says `"index":"refreshing"`, the
answer reflects the graph from before the latest change — ask again in a moment for
anything that depends on it. `ingest_code` is for repos that were never indexed.

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

Pick the first surface that is available, in this order:

1. **MCP tools** — if the `grag` MCP server is configured, the 10 tools appear directly.
2. **REST** — if `grag serve` is running (default `http://127.0.0.1:8471`):
   `POST /api/{query,search,context,ingest,ingest/code}`, `GET /api/{schema,graph/sample,health}`,
   `POST /api/{schema/define,nodes/upsert,edges/upsert}`.
3. **Python** — `from grag.service import GragService` with `GragConfig(db_path=...)`.
   Methods mirror the tools exactly.

If none are running and a `.lbdb` exists, start one:
`GRAG_EMBED_PROVIDER=fastembed grag --db <file> serve --with-mcp` (UI + REST + MCP, with semantic search)
or `grag --db <file> serve` (UI + REST only, FTS-only search).

## If grag stops responding (it heals itself — just retry)

A tool error like "connection refused" or "did not become ready" means the
server crashed or was never started — **not** that your query was wrong. The
MCP proxy supervises the server: it detects a dead upstream, restarts the
daemon, and rebuilds the session in-band. **Wait a few seconds and retry the
same tool call once** — that is the entire recovery procedure for the common
case. A crash never loses committed data (the WAL rolls back to the last
checkpoint at worst). If the server was stopped **on purpose** (`grag stop`),
the proxy respects that and exits instead of restarting it — reconnect the
MCP server or `grag --db <file> start` to resume.

Only if retries keep failing for more than ~30 seconds, recover with the CLI
using the same `--db` path that `grag init` wrote into the MCP config:

1. `grag --db <file> status` — is a server registered, is its pid alive, which port?
2. `grag --db <file> restart` — detached daemon; then retry the tool once.
3. Still failing? Read the daemon log `~/.grag/logs/<name>-<id>.log` (its path
   is also printed by `grag start`) and act on the actual error:
   - "Could not set lock" → another process owns the `.lbdb` (single-writer).
     Use the running server; never start a second writer.
   - "Corrupted wal file" → the previous process was killed mid-write. Delete
     `<file>.wal` (rolls back to the last checkpoint — committed data is safe)
     and start again.
   - "port ... serving a different database" → `grag stop` that server, or pick
     another `--port`.
4. If the server is healthy but the MCP session stays broken, fall back to REST
   with curl (`POST http://127.0.0.1:<port>/api/search` etc. mirror the tools)
   and ask the user to reconnect the MCP server in their client.
5. Only if all of that fails: tell the user what you tried and show the log
   excerpt. Never silently retry-loop.

## The 10 tools

| tool | use |
|---|---|
| `describe_schema` | **Call first**, before writing any Cypher. Returns tables, properties, row counts, sample keys as prompt-shaped text. Prevents hallucinated labels. |
| `define_schema` | Create node/rel tables. Design the graph for the domain — but reuse first: near-duplicate names (case/plural/punctuation of an existing table) are refused with the existing name in the hint; `allow_similar=true` only for a genuinely different concept. |
| `upsert_nodes` / `upsert_edges` | Idempotent MERGE writes; `_source`/`_created_at` added automatically. |
| `cypher_query` | Read-only Cypher. Write keywords (CREATE/MERGE/SET/DELETE/...) are rejected — use the upsert tools for writes. |
| `search_knowledge` | Hybrid BM25 + vector seeds, RRF fusion, **per-label diversity cap**, k-hop expansion, token-budgeted cited context. The main RAG entry point. |
| `get_context` | Re-pack chosen node ids (from a prior search) into a fresh token budget. |
| `ingest_code` | Index a repo's code STRUCTURE (Python, TS/JS/Vue, C#, Terraform, Go, Bash, Java, Kotlin, Rust, C/C++, Ruby, PHP, Swift, Lua, Scala, SQL): Repo/Module/Class/Function/TerraformModuleCall nodes + CONTAINS_*/IMPORTS/INHERITS/CALLS edges. Structure only — never source bodies. |
| `ingest_docs` | Index Markdown/text files on the server as a graph: `Document -> Section` from the heading hierarchy (`SUBSECTION_OF`, `NEXT_SECTION`), body chunks `IN_SECTION`, and `MENTIONS_FUNCTION/CLASS/MODULE` edges for backtick-mentioned code symbols. Run `ingest_code` first so those resolve. Use for specs and design docs. |
| `job_status` | Poll a background ingest (`ingest_code` / `ingest_docs` with `background=true`) by id — use background mode for large trees so the call returns immediately. |

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
   files/symbols/edges; a serving grag does this by itself when the checkout moves.
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
  prefixes automatically. After changing model or text policy: `grag reindex`.
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
