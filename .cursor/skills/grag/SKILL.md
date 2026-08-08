---
name: grag
description: >-
  Query and build a local graph knowledgebase (grag) for grounded, low-hallucination
  answers. Use when the user asks what the project/codebase knows, wants to "remember"
  a fact or decision, asks how components relate, or wants documents turned into a
  searchable knowledge graph. Also use for RAG grounding: search before answering
  architecture/history/rationale questions about a project that has a .lbdb file.
---

# grag — LLM-first graph knowledgebase

grag is an embedded Cypher graph DB (LadybugDB) wrapped in an MCP/REST tool contract
designed for LLM grounding. One `.lbdb` file per database, zero daemons.

**Core loop:** search/traverse the graph to ground an answer, or build the graph by
defining a schema and upserting nodes/edges. Every fact carries `_source` provenance.

## How to talk to it

Pick the first surface that is available, in this order:

1. **MCP tools** — if the `grag` MCP server is configured, the 7 tools appear directly.
2. **REST** — if `grag serve` is running (default `http://127.0.0.1:8471`):
   `POST /api/{query,search,context,ingest}`, `GET /api/{schema,graph/sample,health}`,
   `POST /api/{schema/define,nodes/upsert,edges/upsert}`.
3. **Python** — `from grag.service import GragService` with `GragConfig(db_path=...)`.
   Methods mirror the tools exactly.

If none are running and a `.lbdb` exists, start one: `grag --db <file> serve` (UI + REST)
or `grag --db <file> mcp` (for MCP clients).

## The 7 tools

| tool | use |
|---|---|
| `describe_schema` | **Call first**, before writing any Cypher. Returns tables, properties, row counts, sample keys as prompt-shaped text. Prevents hallucinated labels. |
| `define_schema` | Create node/rel tables. Design the graph for the domain. |
| `upsert_nodes` / `upsert_edges` | Idempotent MERGE writes; `_source`/`_created_at` added automatically. |
| `cypher_query` | Read-only Cypher. Write keywords (CREATE/MERGE/SET/DELETE/...) are rejected — use the upsert tools for writes. |
| `search_knowledge` | Hybrid BM25 + vector seeds, RRF fusion, k-hop expansion, token-budgeted cited context. The main RAG entry point. |
| `get_context` | Re-pack chosen node ids (from a prior search) into a fresh token budget. |

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
- **Properties starting with `_` are grag-internal** (provenance, vector codes). The
  mutation layer rejects caller writes to them.
- **Node ids are `Label:key`** (e.g. `Module:core.engine`). Use `split_node_id` /
  `make_node_id` conventions when correlating ids with primary keys.
- **Errors are self-describing:** tool output is `ERROR: ... HINT: ...`. Read the hint
  and retry — it's written for you.

## Operational gotchas

- **Single-writer:** the embedded engine holds a write lock per `.lbdb`. Do not run
  `serve` and `mcp` against the same file at once; use one process or separate files.
- **FTS index is built lazily** on first search of a table (a write-path cost). Warm it
  with one search before timing latency.
- **Extensions are preloaded at engine startup.** A table with an FTS or HNSW index
  rejects reads/writes/index-maintenance while its extension is unloaded in-process
  ("Trying to insert into an index ... but its extension is not loaded"). `Engine.__init__`
  LOADs FTS + VECTOR once (tolerant when offline), so no path needs per-call handling.
  Extensions are scoped to the Database, so the write conn's LOAD covers pooled readers.
- **Buffer pool:** creating FTS indexes needs headroom; tests use a 128MB pool.
- **Vectors:** default codec `int8` (4x smaller, ~0.998 recall). `polar` is experimental
  (opt-in via `GRAG_VECTOR_CODEC`). Without an embedder, everything works FTS-only.

## Enabling semantic (vector) search

Hybrid search is off until you give it an embedder. The supported path is **fastembed**
(ONNX Runtime — *no* PyTorch), which keeps grag light:

```bash
pip install -e ".[embed-local]"            # fastembed + onnxruntime, ~50-100MB
GRAG_EMBED_PROVIDER=fastembed grag --db knowledge.lbdb serve
```

- Model defaults to `BAAI/bge-small-en-v1.5` (384-dim); override with
  `GRAG_EMBED_MODEL` / `GRAG_EMBED_DIM`. Downloads once to a cache (~50MB), then offline.
- First search downloads the model and embeds all pending nodes (one-time, seconds);
  steady state is ~300ms/query on CPU. Server RSS rises from ~120MB to ~530MB.
- Nodes are (re)embedded lazily whenever their embedding is NULL; text = the node's
  STRING properties joined. New/updated nodes get embedded on the next search.
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
