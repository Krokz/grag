# Servers and multiple clients

A server is never required — the CLI and library work directly on the `.lbdb` file. But when you want the browser UI or a shared MCP endpoint, grag can run one as a background daemon (a convenience front-end over the same embedded engine, not a database service you have to operate). Daemons — whether auto-served for an MCP client or started explicitly — register themselves, so you never have to hunt processes:

```bash
grag --db ~/.grag/myproj.lbdb start    # launch in the background, frees the terminal
grag --db ~/.grag/myproj.lbdb restart  # relaunch (picks up new code after an upgrade/edit)
grag --db ~/.grag/myproj.lbdb status   # running? where? which port/log? + every server on the system
grag --db ~/.grag/myproj.lbdb stop     # stop this database's server
grag stop --all                        # stop every grag server on the system
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
The grace period is 10 seconds across the server's databases. If work outlasts it,
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

In the development version after 0.8.0, CLI `remember`, `search`, `context`,
`ingest` and `ingest-code` locate the selected database's registered owner and use
its API, including its host and bearer token. They open the file directly only
when no matching server is available. A direct stdio session that owns the file
must be closed before changing to init's shared setup; an ownership error explains
that step. A failed server request never falls back to opening a second writer.
After upgrading, restart the owner before using the new ingestion scope options.

There are two distinct ways to hold several projects, depending on whether they **relate**:

**A. Related projects → one shared `.lbdb`.** Ingest several repos into the *same* database and they become separate `Repo` nodes in a single queryable graph — so the LLM can trace a call or an import across repo boundaries, or link a `Decision` in one project to a `Function` in another. This is the model for a monorepo, a system split across services, or any set of codebases that reference each other.

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
grag --db-dir ~/kb serve    # one process serves every .lbdb in ~/kb
```

Every `/api/*` endpoint accepts `?db=<name>` or an `x-grag-db: <name>` header (query param wins). `GET /api/dbs` returns `{"dbs": ["alpha","beta"], "default": "alpha"}` (`{"dbs": [], "default": null}` in single-db mode). Without a selector the server prefers the file matching `db_path`'s name, else a lone `.lbdb`, else 400 with a hint; unknown name → 404 listing available DBs.

For MCP, several IDE windows on one DB collide: stdio spawns a `grag mcp` process per client and LadybugDB allows only ONE process to write a given `.lbdb` ("Could not set lock"). One shared HTTP server avoids it — each window sends its project name via `x-grag-db`:

```bash
grag --db-dir ~/kb mcp --transport streamable-http --host 127.0.0.1 --port 8472
```

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

**HTTP security posture.** The REST layer has no accounts or sessions; the trust model is "whoever can reach the port directly is trusted." Drive-by browser access is denied by default: a Host-header allow-list (loopbacks + the bind host) blocks DNS rebinding, and CORS grants no cross-origin access at all unless you opt in via `GRAG_CORS_ORIGINS` (the built-in UI is served same-origin and needs none). If you bind a non-loopback address, set `GRAG_API_TOKEN` — every public `/api/*` route except `/api/health` and every MCP request then requires `Authorization: Bearer <token>`. The hidden managed-daemon stop hook is not a public API: it accepts only the separate high-entropy token stored in that daemon's private `0600` registration file. Standalone HTTP MCP refuses to bind a non-loopback host without `GRAG_API_TOKEN`. On POSIX, grag also enforces `0600` on database and WAL files (and `0700` when it creates a new database directory).

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

- **Remote proxy mode.** `grag mcp --server-url URL` (env `GRAG_SERVER_URL`) bridges stdio to the remote server, never spawns a local daemon, and when the server restarts it waits, reconnects and replays the MCP handshake — the client sees at most one failed tool call. It pins the server's `database_id` on first contact and refuses to silently bridge onto a different database. Plain `http://` to a non-loopback host is refused unless `GRAG_ALLOW_INSECURE_HTTP=1`.
- **Ingest never stalls searches.** `ingest_code` is incremental: every file is parsed (cross-file `IMPORTS`/`CALLS` need the whole set) but only files whose content hash changed touch the write lock. Embeddings are produced by a background worker in the serving process (`/api/health` → `embedding`); neither ingest nor search embeds on the request thread. Long ingests go through `POST /api/jobs/ingest/code` (or `ingest_code(background=true)` + `job_status`) and return a job id.
- **Specs become a graph.** `grag ingest --sections doc.md` (MCP: `ingest_docs`) turns the heading hierarchy into `Document → Section` nodes (`SUBSECTION_OF`, `NEXT_SECTION`), chunks each section's body under it (`Chunk -IN_SECTION-> Section`, so a hit always cites its section path), and links backtick-mentioned symbols that exist in the code graph (`MENTIONS_FUNCTION` / `MENTIONS_CLASS` / `MENTIONS_MODULE`). Empty `IMPLEMENTS` (Function→Section) / `IMPLEMENTS_CLASS` tables are defined for an agent to fill with the semantic spec↔code links.
- **Online backup.** `GET /api/export` (CLI: `grag export --url URL -o backup.jsonl`) captures one committed state including history and retry receipts, then streams the completed snapshot. Writes pause during capture, not during download; the CLI validates completion before publication. [Restore into a separate verified copy](recovery.md). Failed WAL replay requires offline `grag recover`; a server launch never silently chooses lossy recovery.
