# Deploying grag as a shared (cloud) server

**Model: one writer, many thin clients.** LadybugDB is an embedded,
single-writer engine, so the `.lbdb` file is owned by exactly one `grag serve`
process. Nobody else ever opens the file: every developer's editor connects
over HTTPS through `grag mcp --server-url` (a stdio↔HTTP proxy that reconnects
when the server restarts), and CI refreshes the code graph through the jobs
API. Do **not** put the `.lbdb` on shared/network storage and open it from
several machines — that is the one way to hit the engine's lock error.

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

Sizing: the buffer pool (`GRAG_BUFFER_POOL_MB`) is the working set; 1–2 GB is
plenty for a 100-page spec plus a few repos. The embedder is ONNX on CPU.

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
it, and links every backtick-mentioned symbol that exists in the code graph
(`MENTIONS_FUNCTION` / `MENTIONS_CLASS` / `MENTIONS_MODULE`). Run the code
ingest first so those resolve. The semantic layer — which function
*implements* which section, which components depend on which — is an agent's
job: the empty `IMPLEMENTS` (Function→Section) and `IMPLEMENTS_CLASS` tables
are defined for it, and the recommended pattern is a fixed ontology defined
once via `define_schema`, then an agent walking sections in order and calling
`upsert_nodes` / `upsert_edges`.

Keeping it fresh: `ci-ingest.sh` queues an incremental re-ingest from CI
(`POST /api/jobs/ingest/code`, poll `GET /api/jobs/{id}`). Only changed files
are rewritten; searches are never blocked by it, and embeddings for new nodes
are produced by the server's background worker (`/api/health` → `embedding`).

## Backups

`backup.sh` streams `GET /api/export` from the live server (no downtime) into
dated, gzipped JSONL. Restore with `grag --db new.lbdb import file.jsonl`.
Because the `.lbdb` format belongs to the storage engine, JSONL is the durable
copy — also keep it before upgrading `ladybug`.

## Failure modes

| what | behaviour |
|---|---|
| server crashes | supervisor restarts it; connected proxies wait up to 60 s, reconnect, replay the MCP handshake — the client sees at most one failed tool call |
| corrupt WAL after a hard kill | `GRAG_WAL_AUTO_RECOVER=1` reopens in tolerant mode (writes since last checkpoint lost, vector indexes rebuilt by the worker) instead of crash-looping |
| long ingest | runs on the jobs thread; reads interleave between its short write-lock holds |
| server repointed to another database | proxies pin `database_id` on first contact and refuse to bridge silently onto a different graph |
