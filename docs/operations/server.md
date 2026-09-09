# Servers and multiple clients

A server is never required — the CLI and library work directly on the `.lbdb` file. But when you want the browser UI or a shared MCP endpoint, grag can run one as a background daemon (a convenience front-end over the same embedded engine, not a database service you have to operate). Daemons — whether auto-served for an MCP client or started explicitly — register themselves, so you never have to hunt processes:

```bash
grag --db ~/.grag/myproj.lbdb start    # launch in the background, frees the terminal
grag --db ~/.grag/myproj.lbdb restart  # relaunch (picks up new code after an upgrade/edit)
grag --db ~/.grag/myproj.lbdb status   # selected server status plus this user's registered servers
grag --db ~/.grag/myproj.lbdb stop     # stop this database's server
grag stop --all                        # stop this user's managed grag servers
grag doctor                            # extras, embedder, server, code-index staleness
```

`grag start` binds a stable per-database port by default (pass `--port` to override, `--no-mcp` for UI+REST only) and inherits your environment, so `GRAG_EMBED_PROVIDER=fastembed grag start` carries the embedder into the daemon.

`grag stop -a`, `grag stop -all`, and `grag stop --all` are equivalent. New
daemons use a private authenticated shutdown channel so the API and embedded
database close cleanly on every platform. After upgrading from grag 0.4.0, its
older pidfiles cannot be health/PID-verified; inspect the PIDs shown by
`grag status`, then use `grag stop --all --force` (or `grag restart --force` for
one target) once. Stop/refusal failures return a non-zero exit code.

Shutdown stops accepting work, marks queued jobs `cancelled`, and drains active
ingestion, code verification, requests, and embedding before closing the database.
Application draining has a 10-second grace period across the server's databases.
The HTTP server first allows up to 10 seconds for transport cleanup, so a stuck
connection cannot prevent application shutdown from beginning. `grag stop` waits
up to 30 seconds for process exit; this is not a forced termination deadline.
If accepted work outlasts the application grace period,
grag reports the pending jobs/worker and keeps the database open until that work
finishes; it does not close native connections underneath a live worker. A stuck
Python/native call can keep the process alive. Check the daemon log and retained
server registration when stop reports that the process has not exited. Cancelled
jobs need resubmission after restart; job records remain process-local.

Python callers can use `service.close(timeout=...)` and `service.shutdown_status()`
to inspect `state`, `engine_closed`, active operations, job outcomes, and the
embedding worker. Repeated close calls wait on the same drain. A closed service
or registry cannot be reused; create a new instance after shutdown completes.

Binding the REST/UI server beyond loopback requires `GRAG_API_TOKEN`; grag now
refuses an unauthenticated `--host 0.0.0.0`, `--host ::`, hostname, or LAN
address. `restart` preserves the recorded host, port, and MCP mode unless you
explicitly override them; a custom `--mcp-path` is preserved as well.

Daemon output lands in `~/.grag/logs/<db>-<id>.log` (not /dev/null), so an embedding failure or startup crash is always diagnosable. `grag doctor` also reports, per ingested repo, whether the code index is behind git HEAD ("index is 3 commit(s) behind — re-run ingest-code").

## One database owner, multiple harnesses

Claude Code and Cursor can use the same graph through one shared server. Run
`grag init --client claude` and `grag init --client cursor` in the same checkout;
they reuse its mapping and port. The configured stdio proxies connect to the owner.
Windows harness restrictions may require [starting that owner in a separate
terminal](../installation.md).

CLI `remember`, `search`, `context`,
`ingest` and `ingest-code` locate the selected database's registered owner and use
its API at the registered host. Set `GRAG_API_TOKEN` in the CLI environment when
that owner requires authentication. They open the file directly only
when no matching server is available. A direct stdio session that owns the file
must be closed before changing to init's shared setup; an ownership error explains
that step. A failed server request never falls back to opening a second writer.
After upgrading, restart the owner before using the new ingestion scope options.

There are two distinct ways to hold several projects, depending on whether they **relate**:

**A. Related projects → one shared `.lbdb`.** Ingest several repos into the same
database to query their separate `Repo` nodes together and link authored decisions
across projects. Automatic cross-repo edges depend on parser coverage; Python,
JS/TS and Go static resolution does not infer arbitrary cross-service calls.

```bash
grag --db platform.lbdb ingest-code ../api ../web ../infra   # 3 repos, one graph
```

```cypher
// cross-repo imports, inside one db
MATCH (r1:Repo)-[:CONTAINS_REPO_MODULE]->(a:Module)-[:IMPORTS]->(b:Module)<-[:CONTAINS_REPO_MODULE]-(r2:Repo)
WHERE r1.id <> r2.id RETURN a.id, b.id
```

**B. Unrelated projects → separate `.lbdb` files.** One file = one isolated universe (no shared entities, no cross-db queries), so a throwaway experiment never pollutes a real project's graph. This is the default local-first pattern: **one `.lbdb` per project, per developer**, each queryable locally with zero per-token retrieval cost. To serve many of them at once, opt into multi-db mode with `--db-dir`:

```bash
grag --db-dir ~/kb serve --with-mcp --port 8472  # shared REST/UI and MCP owner
```

Database-scoped REST endpoints accept `?db=<name>` or an `x-grag-db: <name>`
header (query param wins). `/api/health` reports the server/default database;
`GET /api/dbs` returns `{"dbs": ["alpha","beta"], "default": "alpha"}`
(`{"dbs": [], "default": null}` in single-db mode). Without a selector the server
prefers the file matching `db_path`'s name, else a lone `.lbdb`, else 400 with a
hint; an unknown name returns 404 listing available databases. Services open lazily.

Point MCP clients at that same server; each window sends its selected database
name through `x-grag-db`. Do not start a separate direct database owner for it.

Cursor / `.cursor/mcp.json` (per window, one header per project):

```json
{
  "mcpServers": {
    "grag": {
      "url": "http://127.0.0.1:8472/mcp",
      "headers": { "x-grag-db": "project-a" }
    }
  }
}
```

The server is localhost-only by default, and db names are routing hints, not auth — resolution rejects absolute paths and `..`. Direct stdio (`grag --db knowledge.lbdb mcp`) opens the database itself and is suitable for a single client. Use init-generated auto-serve proxies for shared access.

**HTTP security posture.** grag has a shared bearer-token boundary, with no
per-user accounts or per-database permissions. Without a token, direct loopback
callers are trusted. A Host-header allow-list (loopbacks plus the bind host)
blocks DNS rebinding, and CORS grants no cross-origin access unless configured
with `GRAG_CORS_ORIGINS`; the same-origin UI needs none. Non-loopback serving
requires `GRAG_API_TOKEN`. When set, every public `/api/*` route except
`/api/health` and every MCP request requires `Authorization: Bearer <token>`.
The managed-daemon stop hook uses a separate token in its private registration.
On POSIX, database and WAL files use `0600`, and newly created database directories
use `0700`.

## Remote / team deployment

grag's engine is embedded and single-writer, so a shared graph is one `grag serve --with-mcp` process that owns the `.lbdb`; nobody else opens the file. Every developer's editor connects to it over HTTPS and CI keeps the code graph fresh through the jobs API. `deploy/` has a Dockerfile, compose file, systemd unit, backup and CI scripts, and a walkthrough.

```bash
# server (Docker; put a TLS proxy in front)
export GRAG_API_TOKEN="$(openssl rand -hex 32)"
docker compose -f deploy/docker-compose.yml up -d --build

# each client: writes .mcp.json with `grag mcp --server-url …` and references
# ${GRAG_API_TOKEN} from the environment (never stores it)
grag init --server-url https://grag.example.com
```

What the server does differently from a laptop:

- **Remote proxy mode.** `grag mcp --server-url URL` bridges stdio to the remote server, never spawns a local daemon, and replays the MCP handshake after reconnecting. Repeated outages can exhaust retries; an interrupted tool call may need explicit retry or stored-state inspection. The proxy pins database identity and refuses a silent switch. Plain HTTP beyond loopback requires `GRAG_ALLOW_INSECURE_HTTP=1`.
- **Background ingestion.** Code parsing and relationship resolution happen before atomic graph publication. Incremental updates include content, parser and dependency changes; unchanged files are still parsed. Queued ingestion lets the HTTP call return a job ID while other readers use committed data. Native resource contention and write-side work can still affect latency. By default, stored-node embeddings run on a separate worker; query embedding remains part of semantic search. Use `POST /api/jobs/ingest/code` or MCP `ingest_code(background=true)`, then poll the job.
- **Specs become a graph.** `grag ingest --sections doc.md` (MCP: `ingest_docs`) turns the heading hierarchy into `Document → Section` nodes (`SUBSECTION_OF`, `NEXT_SECTION`), chunks each section's body under it (`Chunk -IN_SECTION-> Section`), and links resolvable backtick-mentioned symbols in the code graph (`MENTIONS_FUNCTION` / `MENTIONS_CLASS` / `MENTIONS_MODULE` / `MENTIONS_CONSTANT`). Empty `IMPLEMENTS` (Function→Section) / `IMPLEMENTS_CLASS` tables are defined for an agent to fill with the semantic spec↔code links.
- **Online backup.** `GET /api/export` (CLI: `grag export --url URL -o backup.jsonl`) captures one committed state including history and retry receipts, then streams the completed snapshot. Writes pause during capture, not during download; the CLI validates completion before publication. [Restore into a separate verified copy](recovery.md). Failed WAL replay requires offline `grag recover`; a server launch never silently chooses lossy recovery.
