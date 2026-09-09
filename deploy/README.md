# Deploying grag as a shared (cloud) server

**Model: one writer, many thin clients.** LadybugDB is an embedded,
single-writer engine, so the `.lbdb` file is owned by exactly one `grag serve`
process. Nobody else ever opens the file: every developer's editor connects
over HTTPS through `grag mcp --server-url` (a stdio↔HTTP proxy that reconnects
when the server restarts), and CI refreshes the code graph through the jobs
API. Keep the database on storage local to that owner. Multiple processes opening
the same file for writes conflict even on one machine; connect additional clients
through the server. See the [architecture guide](../docs/architecture.md).

```
 developer laptops                         cloud host
 ┌──────────────┐  stdio  ┌────────────┐   HTTPS   ┌────────────────────────────┐
 │ Claude Code  │◀──────▶│ grag mcp   │◀────────▶│ TLS proxy → grag serve      │
 │ Cursor / Zed │        │ --server-url│          │   --with-mcp (single writer)│
 └──────────────┘        └────────────┘          │   embed worker (background) │
                                                  │   jobs thread (ingests)     │
 CI (push to algo4) ── POST /api/jobs/ingest/code ▶   /data/grag.lbdb           │
                                                  └────────────────────────────┘
```

## Server

Docker (recommended):

```bash
cd ui && npm ci && npm run build && cd ..          # UI assets go into the wheel
export GRAG_API_TOKEN="$(openssl rand -hex 32)"
docker compose -f deploy/docker-compose.yml up -d --build
```

Or systemd on a VM: see `grag.service`. Either way put a TLS-terminating
reverse proxy in front (Caddy: `grag.example.com { reverse_proxy 127.0.0.1:47832 }`).
`GRAG_API_TOKEN` is mandatory off loopback; every `/api/*` route except
`/api/health` and the whole `/mcp` mount require `Authorization: Bearer`.

Size the host against a representative ingest and concurrent retrieval workload.
`GRAG_BUFFER_POOL_MB` bounds the database buffer pool, not total process memory:
parsing, query results and an optional embedding model need additional memory.
The optional FastEmbed provider uses ONNX on CPU.

## Clients

```bash
export GRAG_API_TOKEN=...                        # in each developer's shell env
grag init --server-url https://grag.example.com  # writes .mcp.json / CLAUDE.md
```

The written config runs `grag mcp --server-url …` and references
`${GRAG_API_TOKEN}` (never stores it). `grag init --url --server-url …`
writes a direct streamable-http entry instead for clients that speak HTTP
MCP natively. Multi-database servers (`--db-dir`): add `--server-db NAME`.

## Loading the graph

```bash
# on the server host (or via the MCP tools ingest_code / ingest_docs):
grag --db /data/grag.lbdb ingest-code /repos/algo4 /repos/algo4-infra
grag --db /data/grag.lbdb ingest --sections /repos/docs/algo-bible.md
```

`ingest --sections` turns the heading hierarchy into `Document → Section`
nodes with `SUBSECTION_OF` / `NEXT_SECTION`, chunks each section's body under
it, and links resolvable backtick-mentioned symbols in the code graph
(`MENTIONS_FUNCTION` / `MENTIONS_CLASS` / `MENTIONS_MODULE` / `MENTIONS_CONSTANT`). Run the code
ingest first so those resolve. The semantic layer — which function
*implements* which section, which components depend on which — is an agent's
job: the empty `IMPLEMENTS` (Function→Section) and `IMPLEMENTS_CLASS` tables
are defined for it, and the recommended pattern is a fixed ontology defined
once via `define_schema`, then an agent walking sections in order and calling
`upsert_nodes` / `upsert_edges`.

Keeping it fresh: `ci-ingest.sh` queues an incremental re-ingest from CI
(`POST /api/jobs/ingest/code`, poll `GET /api/jobs/{id}`). Files are parsed before
atomic graph publication; content, parser and dependency changes determine which
generated records need rewriting. Readers see committed graph state, but resource
contention can affect latency. With embeddings configured, the default background
worker embeds new nodes (`/api/health` → `embedding`).

## Backups

`backup.sh` captures a consistent snapshot from the live server into dated,
gzipped JSONL. Writes pause during capture; download uses a completed spool. The
CLI checks completion before publishing. Graph provenance, source ownership,
history and retry receipts are preserved; vectors/indexes rebuild. After
decompression, restore with `grag --db new.lbdb import file.jsonl`. The destination
must be new and is published only after checkpoint, strict reopen and full-content
verification. [Backup and restore details](../docs/operations/recovery.md) explain
legacy exports, staging files and snapshot-point continuity. Keep a verified export
before upgrading `ladybug`.

## Failure modes

| what | behaviour |
|---|---|
| server crashes | the supervisor restarts it; proxies wait up to 60 s for the server and rebuild the MCP handshake. Reconnection can exhaust its retries. An interrupted write needs its original operation receipt or a stored-state check before retrying. |
| WAL/shadow replay failure | startup fails with recovery guidance; stop the supervisor and run `grag --db <file> recover` offline. Originals are preserved and recovery produces a separate verified copy. Partial replay requires `--allow-data-loss`; committed writes may be lost. Review the copy before repointing the server. |
| long ingest | runs on the jobs thread; parsing precedes one atomic graph publication. Readers use committed state; other writes serialize and resource contention can affect read latency. |
| server repointed to another database | proxies pin `database_id` on first contact and refuse to bridge silently onto a different graph |

`GRAG_WAL_AUTO_RECOVER` is deprecated and no longer permits in-place lossy recovery.
Do not remove WAL/shadow files by hand. Keep the recovery bundle (raw snapshots,
checksums, manifest, and failed attempts) until the recovered data has been reviewed.
`grag reindex` rebuilds embeddings in an already-openable database; it is not a
replacement for recovery. Rebuildable code structure and agent-authored memories
can share a database, so re-ingestion alone does not restore lost decisions.
