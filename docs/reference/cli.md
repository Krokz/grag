# CLI commands

Look up flags by task. Use `grag --help` or `grag <command> --help` for the
command syntax supported by your installed version.
{ .grag-lead }

| Task | Commands |
|---|---|
| Connect | `init`, `relocate` |
| Index | `ingest-code`, `ingest` |
| Save and retrieve | `remember`, `inspect`, `retire`, `search`, `context`, `memory adopt` |
| Run a server | `serve`, `mcp`, `start`, `restart`, `stop` |
| Diagnose | `status`, `doctor` |
| Preserve data | `export`, `import`, `recover` |
| Maintain embeddings | `reindex`, `bench` |

!!! info "Version availability"
    `inspect`, `retire` and other options marked **Since 0.10.0** require grag 0.10.0 or later.
    The installed command's help is authoritative for your environment.

## Everyday memory and retrieval


These commands use the same database resolution and owner routing as ingestion:

| Command/option | Default | Behavior |
|---|---|---|
| `remember TEXT` | `Memory` label | Saves text in a compatible `id,text` memory table. `--id` reuses a key; omitted IDs get a UUID. |
| `remember --label NAME` / `--source SOURCE` | `Memory` / `grag remember` | Selects the table and provenance. |
| `remember --expected-revision TOKEN` | unset | Guards an edit against the revision returned by an earlier read. |
| `remember --track-history` / `--reason TEXT` | off / unset | Since 0.10.0: either enables correction history; create-only without an explicit revision guard. |
| `remember/retire --operation-id ID` | unset | Since 0.10.0: durable receipt for an exact retry; `remember` requires `--id`. Replay revisions describe the original commit. |
| `inspect Label:key` | — | Since 0.10.0: reads current content and revision, including excluded evidence. Ordinary query limits apply; no context token budget. |
| `retire Label:key --expected-revision TOKEN` | required existing token | Since 0.10.0: retracts from current retrieval while preserving content, links and history; clears a supersession pointer. `--reason` records why; optional `--source` replaces provenance. |
| `search QUERY --label NAME` | all searchable labels | Searches the selected graph; repeat `--label` to narrow it to several labels. |
| `context Label:key [...]` | — | Fetches context for the supplied canonical node IDs. |
| `context --history` / `--revision N` | off / unset | Since 0.10.0: list history or read one snapshot sequence, for one ID; mutually exclusive. |
| `context --history-before N` | unset | Since 0.10.0: continue `--history` using its `next_before` cursor. |
| `search/context --evidence MODE` | `current` | Since 0.10.0: `all` includes qualified superseded/retracted/expired/disputed evidence. |
| `search/context --tokens N` | `2000` | Estimated retrieval budget; this CLI default overrides `GRAG_TOKEN_BUDGET` for these commands. |
| `search/context --hops N` | `1` | Graph-expansion depth. |
| `search/context/inspect --freshness MODE` | `allow_stale` | `allow_stale`, `wait` or `require`; uses the request's default freshness timeout. |
| `memory adopt` | — | Since 0.13.0: adopts the optional, versioned [memory preset](schema.md#optional-memory-preset); additive and repeatable. Conflicts are reported and left unchanged. `--allow-similar` creates preset tables beside near-duplicates. |
| `remember/search/context/inspect/retire/memory adopt --json` | off | Emits compact machine-readable JSON instead of the human summary (`inspect` otherwise uses indented JSON). |

See the [everyday memory walkthrough](../guides/memory.md#cli-shortcuts) for guarded
corrections, retry semantics and the difference between retraction and deletion.


## Select a database

| Command/option | Default | What it affects |
|---|---|---|
| global `--db PATH` | environment, project mapping, legacy registration, then root `knowledge.lbdb` | Selects one database file for any command. Mutually exclusive with `--db-dir`. |
| global `--db-dir DIR` | `GRAG_DB_DIR` or unset | Selects a directory of databases for multi-db serving. Mutually exclusive with `--db`. |

## Connect an agent

| Command/option | Default | What it affects |
|---|---|---|
| `init --client CLIENT` | `auto` | Client to configure: `claude`, `cursor`, `windsurf`, `zed`, or auto-detection. grag 0.10.0 also accept `codex` for skill installation; project setup requires `--no-mcp` because Codex MCP registration is managed separately. |
| `init --port PORT` | saved, otherwise derived per-project | Port written into generated MCP/shared-server configuration. New defaults derive from the database path (41000–49151); choose an explicit port if occupied. |
| `init --ingest` | off | Also runs `ingest-code` on the resolved project root immediately. |
| `init --ingest-if-empty` | off | Since 0.10.0: checks the selected local graph and sets up/indexes the checkout only when absent, empty or containing only the verified init marker. Other content skips setup and indexing. Mutually exclusive with `--ingest`; refuses remote servers and `--remove`. |
| `init --memory-preset` | off | Since 0.13.0: also runs `memory adopt` on the selected local database after setup. Refuses remote servers and `--remove`. |
| `init --global-skill` | off | Since 0.10.0: installs only the user-level skill bundle so it can be invoked in new repos. Supports `--client`, `--dry-run` and `--remove`; does not open a database or configure MCP. Claude honors `CLAUDE_CONFIG_DIR` for its personal skill location. |
| `init --remove` | off | Undoes init: removes the grag MCP entry and the CLAUDE.md block. |
| `init --url` | off | Writes direct HTTP URL transport instead of stdio plus auto-serve; the shared server must already be running. |
| `init --server-url URL` | unset | Registers a remote grag server instead of a local database: MCP config runs `grag mcp --server-url` (or, with `--url`, points at the server's `/mcp/` with a bearer header), referencing `${GRAG_API_TOKEN}` rather than storing it; CLAUDE.md documents the shared graph. `--server-db` selects a multi-db database. |
| `init --no-mcp` | off | Skips MCP client configuration. |
| `init --no-claude-md` | off | Skips the `CLAUDE.md` guidance block. |
| `init --no-verify` | off | Writes configuration without the MCP tool-list/write/read verification; explicitly leaves connection readiness unverified. |
| `init --dry-run` | off | Shows actual file diffs, including removal, without creating files or backups. |
| `relocate OLD_ROOT NEW_ROOT` | — | Reconciles a moved checkout's existing database and local registrations. Retains graph IDs and saved relationships; never moves or creates a database. |
| `relocate ... --dry-run` | off | Previews graph path/settings changes and client-file diffs without writing. |

## Ingest source material

| Command/option | Default | What it affects |
|---|---|---|
| `ingest --sections` | off | Section-aware Markdown ingest: `Document → Section` nodes from the heading hierarchy, chunks linked `IN_SECTION`, backtick-mentioned code symbols linked to the code graph. Paths may be directories. |
| `ingest --json-mode records\|document` | `records` | New in 0.10.0: explicitly index ordinary `.json` as validated source text with `document`. The default imports document records; JSONL always remains records. [Formats and limits](../guides/documents.md). |
| `ingest-code --no-calls` | off | Disables `CALLS` extraction for supported Python, JS/TS and Go analysis; retains structure, imports and other supported relationships. |
| `ingest-code --root PATH` | inferred | Enclosing source root for selected paths; retains stable module IDs and scope boundaries. |
| `ingest-code --replace-scope` | off | With `--root`, replaces its saved paths; no paths unregisters that code scope. Authored evidence is preserved during reconciliation. |
| `ingest-code --max-file-kb N` | `1024` | Skips source files larger than this many KiB. |

## Serve and connect MCP

| Command/option | Default | What it affects |
|---|---|---|
| `serve --host HOST` | `127.0.0.1` | REST/UI bind address and Host-header allow-list. Set `GRAG_API_TOKEN` before using a non-loopback address. |
| `serve --port PORT` | saved project port, otherwise `8471` | REST/UI listening port. Explicit database selectors bypass the saved project port. |
| `serve --with-mcp` | off | Mounts MCP in the REST/UI process so all surfaces safely share the embedded database's one writer. |
| `serve --mcp-path PATH` | `/mcp` | Mount path used with `--with-mcp`. |
| `mcp --transport MODE` | `stdio` | Chooses `stdio` or `streamable-http`. |
| `mcp --host HOST` | `127.0.0.1` | Standalone HTTP MCP bind address. A non-loopback address requires `GRAG_API_TOKEN`. |
| `mcp --port PORT` | saved project port, otherwise `8471` | Standalone HTTP MCP port, or the shared server target for `--auto-serve`. Explicit database selectors bypass the saved project port. |
| `mcp --path PATH` | `/mcp` | Standalone streamable HTTP endpoint path. |
| `mcp --auto-serve` | off | Keeps the client transport on stdio but proxies it to a shared `serve --with-mcp` process, starting that process when needed. |
| `mcp --server-url URL` | `GRAG_SERVER_URL` or unset | Proxies stdio to a remote, already-running grag server (cloud host). Never spawns a daemon; waits and reconnects across server restarts; pins the server's `database_id`. Takes precedence over `--auto-serve`. |
| `mcp --server-db NAME` | `GRAG_SERVER_DB` or unset | Database name sent as `x-grag-db` to a multi-db remote server. |
| `mcp --insecure-http` | off | Allow a plain-http `--server-url` to a non-loopback host. |

## Manage the owner

| Command/option | Default | What it affects |
|---|---|---|
| `start --host HOST` | `127.0.0.1` | Starts a managed background REST/UI server on this host. Non-loopback binds require `GRAG_API_TOKEN`. |
| `start --port PORT` | saved project port, otherwise per-database derived port | Port for the managed background server. |
| `start --no-mcp` | off | Starts the managed server without its normally enabled MCP endpoint. |
| `start --mcp-path PATH` | `/mcp` | Mounted MCP path for the managed background server. |
| `restart --host HOST` | preserve current | Overrides the registered bind host while restarting. |
| `restart --port PORT` | preserve current | Overrides the registered port while restarting. |
| `restart --with-mcp` / `--no-mcp` | preserve current | Enables or disables mounted MCP while restarting. |
| `restart --mcp-path PATH` | preserve current | Overrides the mounted MCP path while restarting. |
| `restart --force` | off | Allows one-time migration of a live legacy registration after independently verifying its PID. |
| `status` | `--json` (since 0.10.0) | Database/owner, runtime and saved-client discovery. Inspection preserves stale registrations and does not open graphs. |
| `stop` | — | Gracefully stops the managed background server for the selected database. |
| `stop -a` / `-all` / `--all` | off | Stops every safely verifiable managed grag server. Refuses unresolved legacy registrations instead of reporting false success. |
| `stop --force` | off | Also permits signaling a live legacy/unverified registration; use only after independently verifying its recorded PID. |
| `doctor` | `--prepare`, `--json`, `--timeout`, `--verify-client CLIENT:SCOPE:NAME` (since 0.10.0) | Isolated install checks plus configuration discovery. `ready` covers installation only. Explicit saved-launcher MCP read requires a matching existing local DB; may start an owner/refresh. Exit 1 for required install, resolution or requested client-check failure. |

## Backup and recovery

| Command/option | Default | What it affects |
|---|---|---|
| `recover --out-dir DIRECTORY` | unique directory beside DB | Requires explicit `--db <file>`. Preserves the offline DB and sidecars with checksums, replays a separate copy, then checkpoints and verifies a strict reopen. Never reuses an existing output directory. |
| `recover --allow-data-loss` | off | After a strict WAL replay failure, permits partial replay on a fresh copy. Committed writes may be missing; loss cannot be quantified automatically. |
| `recover --timeout SECONDS` | `300` | Deadline for each snapshot/replay child process. Failure or timeout leaves the source and preserved recovery material available. |
| `export --out FILE` | stdout | Captures and validates a consistent format-2 JSONL snapshot, including history and retry receipts. File publication is atomic; vectors/indexes are rebuilt. |
| `export --url URL` | `GRAG_SERVER_URL` or unset | Explicit server override for online snapshot capture (bearer from `GRAG_API_TOKEN`). Without this flag, `--db` automatically uses its registered owner when available. `--server-db` selects a multi-db database. |
| `import --allow-legacy` | off | Explicitly accepts v1 exports without completion proof, history, receipts or internal ownership/lifecycle state. |
| `import FILE` | — | Restores into a new local `--db` file; validates completion, restores transactionally in staging, then checkpoints and verifies all contents after strict reopen before publication. Existing destinations are refused. |

## Embedding maintenance

| Command/option | Default | What it affects |
|---|---|---|
| `bench --codec CODEC` | all codecs | Benchmarks only the named codec; without it, the benchmark runs `fp32`, `int8`, `binary`, and `polar`. |
| `reindex --batch-size N` | `128` | Number of nodes embedded per reindex batch. |

[Environment settings and database resolution →](configuration.md)
