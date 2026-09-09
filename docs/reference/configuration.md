# Configuration

The CLI starts from `GragConfig.from_env()`, then an explicitly supplied CLI
option wins where that command offers one. Constructing `GragConfig(...)` in Python
uses the values you pass plus the model defaults; it does **not** read the environment
unless you call `GragConfig.from_env()`.

## Environment variables

| Variable | Accepted values | Default | What it affects |
|---|---|---|---|
| `GRAG_DB_PATH` | Filesystem path | `knowledge.lbdb` | Database used in single-database mode. In multi-db mode, its filename identifies the preferred default database. Overridden by global CLI option `--db`. |
| `GRAG_DB_DIR` | Directory path | unset | Enables multi-database mode: short database names resolve to `<dir>/<name>.lbdb`. Overridden by global CLI option `--db-dir`. |
| `GRAG_BUFFER_POOL_MB` | Integer MiB | `256` | LadybugDB buffer-pool memory. Raise it for large imports/index builds; lower it to reduce resident-memory pressure. This is not the database file size. |
| `GRAG_STATEMENT_TIMEOUT_MS` | Integer 0–2147483647 | `30000` | Cooperative native execution limit per ordinary statement, in milliseconds. `0` disables it. Commit, rollback and checkpoint always finish without this deadline. Set it on the owning server and restart; another client's environment does not reconfigure a running owner. |
| `GRAG_TOKEN_BUDGET` | Integer 256–32768 | `2000` | Estimated budget for the complete retrieval payload, including graph and metadata; request-level `token_budget` wins. Not a model-token guarantee. |
| `GRAG_SEARCH_LABEL_CAP` | Integer | `2` | Maximum fused search seeds contributed by one node label before other labels get a turn. Prevents large tables such as `Function` from crowding out `Decision`/`Concept`. Set `0` or a negative value to disable diversity capping and use pure fused rank order. |
| `GRAG_VECTOR_CODEC` | `fp32`, `int8`, `binary`, `polar` | `fp32` | Storage/candidate-generation codec. `fp32` uses exact cosine scanning; compressed codecs scan compact codes and exactly rescore shortlisted fp32 vectors. Changes make existing vectors pending for automatic rebuilding. |
| `GRAG_POLAR_BITS_PER_DIM` | Float in `(0, 8]` | `1.0` | Approximate angular bits per vector dimension when `GRAG_VECTOR_CODEC=polar`. Higher values improve reconstruction at the cost of larger codes. Read directly by the polar codec. |
| `GRAG_MAX_EMBED_PER_SEARCH` | Non-negative integer | `256` | Maximum pending nodes embedded synchronously by one search when no background worker is running (`GRAG_EMBED_BACKGROUND=0`, or library use without a serving process). Remaining work is reported as `pending_embeddings`. |
| `GRAG_EMBED_BACKGROUND` | `1`/`0` | `1` | Serving processes run a background embedding worker per database, so ingests and searches never embed on the request thread. `0` restores inline embedding (search embeds up to `GRAG_MAX_EMBED_PER_SEARCH`; ingest embeds its own writes). |
| `GRAG_SERVER_URL` | `https://host[:port]` | unset | Remote-server mode: `grag mcp` proxies stdio to this already-running grag server instead of auto-serving a local daemon, and `grag export` streams `GET /api/export` from it. The proxy never opens a `.lbdb`; it reconnects and replays the MCP handshake when the server restarts. |
| `GRAG_SERVER_DB` | Database name | unset | With `GRAG_SERVER_URL`: the `x-grag-db` header for a multi-db (`--db-dir`) server. |
| `GRAG_ALLOW_INSECURE_HTTP` | `1`/`0` | `0` | Permit a plain-`http://` `GRAG_SERVER_URL` to a non-loopback host (the bearer token then travels unencrypted). |
| `GRAG_WAL_AUTO_RECOVER` | `1`/`0` | `0` | Deprecated compatibility setting; no longer enables lossy recovery on startup. Use offline `grag --db <file> recover`, optionally with explicit `--allow-data-loss`. |
| `GRAG_EMBED_PROVIDER` | `fastembed` or `remote` | unset | Enables vector search. Unset means BM25/FTS-only retrieval. `fastembed` is local; `remote` sends embedding input to the configured OpenAI-compatible service. |
| `GRAG_AUTO_REFRESH_CODE` | `1`/`0` | `1` | Serving processes re-ingest an indexed checkout automatically when its git state moved (incremental, on the job thread). |
| `GRAG_AUTO_REFRESH_INTERVAL_S` | Seconds | `30` | Minimum time between drift checks. |
| `GRAG_EMBED_THREADS` | Integer | `min(4, cores)` | ONNX Runtime threads for the local embedder. `1` restores the conservative setting from before onnxruntime 1.29. |
| `GRAG_EMBED_QUERY_PREFIX` / `GRAG_EMBED_DOC_PREFIX` | String | by model family | Retrieval prefixes prepended to queries / node texts before embedding. Unset picks the family default (bge, nomic, e5, arctic, mxbai); empty string disables. Changes trigger automatic rebuilding. |
| `GRAG_EMBED_EXCLUDE_PROPS` | Comma list | `meta,path,heading_path,language,git_commit,git_branch,ingested_at` | STRING properties left out of the embedding text (they stay in the FTS index). Reindex after changing. |
| `GRAG_EMBED_MODEL` | Provider model name | `BAAI/bge-small-en-v1.5` | Embedding model identifier, used only when `GRAG_EMBED_PROVIDER` is set. Changing it invalidates/rebuilds affected embeddings lazily. |
| `GRAG_EMBED_DIM` | Positive integer | `384` | Embedding vector width. It must match the selected model's actual output dimension and the stored vector column. |
| `GRAG_EMBED_BASE_URL` | URL | unset | OpenAI-compatible endpoint root for the `remote` provider; required when using a remote embedding service. |
| `GRAG_EMBED_API_KEY_ENV` | Name of another environment variable | unset | Tells the remote provider which environment variable contains its bearer API key. This value is a variable **name**, not the secret itself. No authorization header is sent when unset. |
| `GRAG_API_TOKEN` | Non-empty bearer token | unset | Requires `Authorization: Bearer <token>` on REST routes except `/api/health`, and on HTTP MCP. A non-loopback standalone HTTP MCP bind is rejected when this is unset. The built-in UI stores a supplied token in that browser only. |
| `GRAG_CORS_ORIGINS` | Comma-separated origins | unset (no cross-origin access) | Adds allowed browser origins for separately hosted clients, for example `https://app.example.com,http://localhost:5173`. The built-in same-origin UI needs no entry; credentials remain disabled. |

Embedding-specific variables are ignored until `GRAG_EMBED_PROVIDER` is set. For
local semantic search, install the optional dependency first:

```bash
pip install 'gragdb[embed-local]'
GRAG_EMBED_PROVIDER=fastembed grag --db knowledge.lbdb serve
```

## Python-only `GragConfig` options

These controls currently have no `GRAG_*` environment equivalent. Pass them when
embedding grag as a Python library; the CLI exposes the server-related subset shown
in the next table.

| `GragConfig` field | Type | Default | What it affects |
|---|---|---|---|
| `max_read_conns` | `int` | `4` | Maximum pooled read connections. Writes still serialize through one write connection. |
| `default_query_limit` | `int` | `100` | Row limit applied when a query/request does not provide one. |
| `max_query_limit` | `int` | `1000` | Server-side ceiling for requested query/search limits. |
| `max_hops` | `int` | `3` | Maximum graph-expansion depth accepted by retrieval/context requests. |
| `mcp_path` | `str \| None` | `None` | Mounts streamable HTTP MCP into the REST/UI app at this path. `None` leaves MCP unmounted. CLI equivalent: `serve --with-mcp --mcp-path /mcp`. |
| `host` | `str` | `127.0.0.1` | Expected bind host used by Host-header/DNS-rebinding allow-lists. The CLI's `serve --host` or `mcp --host` sets the runtime bind. |

The remaining `GragConfig` fields map directly to the environment table:
`db_path`, `db_dir`, `buffer_pool_size` (bytes rather than MiB),
`default_token_budget`, `statement_timeout_ms`, `search_label_cap`, `vector_codec`, `embedder`,
`api_token`, `cors_origins`, `max_embed_per_search`, `embed_in_background`, `server_url`,
`server_db`, `allow_insecure_http`, and `wal_auto_recover`. `EmbedderConfig` contains
`provider`, `model`, `dim`, `base_url`, and `api_key_env`, with the same meanings and
defaults listed above.

Native timeout checks are cooperative: compilation, allocation and some operators
can run past the configured interval before checking it. This is not a hard wall-clock
limit on a tool call, lock wait, whole ingestion or shutdown. Query result/work limits
still apply. Python configuration rejects negative values, booleans and non-integers;
environment configuration rejects invalid integers before opening the database.

`grag doctor` reports the actual Ladybug backend and tests the selected local timeout.
The owning server's `/api/health` reports its `engine.backend`, version, effective
`statement_timeout_ms`, `completion_timeout_ms` and `writer_state`. Recreate a Python
Engine to apply configuration changes. Both pybind and the pinned Ladybug 0.20.3
C-API backend use native limits; grag disables that version's erroneous Python
10 ms watchdog and synthetic range-query interrupt on its own connections only.

## CLI options

Use `grag --help` and `grag <command> --help` for the authoritative command syntax.
The configuration-affecting options are:

| Command/option | Default | What it affects |
|---|---|---|
| global `--db PATH` | environment, project mapping, legacy registration, then root `knowledge.lbdb` | Selects one database file for any command. Mutually exclusive with `--db-dir`. |
| global `--db-dir DIR` | `GRAG_DB_DIR` or unset | Selects a directory of databases for multi-db serving. Mutually exclusive with `--db`. |
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
| `ingest --sections` | off | Section-aware Markdown ingest: `Document → Section` nodes from the heading hierarchy, chunks linked `IN_SECTION`, backtick-mentioned code symbols linked to the code graph. Paths may be directories. |
| `start --host HOST` | `127.0.0.1` | Starts a managed background REST/UI server on this host. Non-loopback binds require `GRAG_API_TOKEN`. |
| `start --port PORT` | saved project port, otherwise per-database derived port | Port for the managed background server. |
| `start --no-mcp` | off | Starts the managed server without its normally enabled MCP endpoint. |
| `start --mcp-path PATH` | `/mcp` | Mounted MCP path for the managed background server. |
| `restart --host HOST` | preserve current | Overrides the registered bind host while restarting. |
| `restart --port PORT` | preserve current | Overrides the registered port while restarting. |
| `restart --with-mcp` / `--no-mcp` | preserve current | Enables or disables mounted MCP while restarting. |
| `restart --mcp-path PATH` | preserve current | Overrides the mounted MCP path while restarting. |
| `restart --force` | off | Allows one-time migration of a live legacy registration after independently verifying its PID. |
| `ingest-code --no-calls` | off | Skips Python `CALLS` edge extraction. |
| `ingest-code --max-file-kb N` | `1024` | Skips source files larger than this many KiB. |
| `bench --codec CODEC` | all codecs | Benchmarks only the named codec; without it, the benchmark runs `fp32`, `int8`, `binary`, and `polar`. |
| `reindex --batch-size N` | `128` | Number of nodes embedded per reindex batch. |
| `recover --out-dir DIRECTORY` | unique directory beside DB | Requires explicit `--db <file>`. Preserves the offline DB and sidecars with checksums, replays a separate copy, then checkpoints and verifies a strict reopen. Never reuses an existing output directory. |
| `recover --allow-data-loss` | off | After a strict WAL replay failure, permits partial replay on a fresh copy. Committed writes may be missing; loss cannot be quantified automatically. |
| `recover --timeout SECONDS` | `300` | Deadline for each snapshot/replay child process. Failure or timeout leaves the source and preserved recovery material available. |
| `status` | — | Shows whether a server is running for the selected database, on which port, and where its log is. |
| `stop` | — | Gracefully stops the managed background server for the selected database. |
| `stop -a` / `-all` / `--all` | off | Stops every safely verifiable managed grag server. Refuses unresolved legacy registrations instead of reporting false success. |
| `stop --force` | off | Also permits signaling a live legacy/unverified registration; use only after independently verifying its recorded PID. |
| `doctor` | `--prepare`, `--json`, `--timeout` | Isolated native/FTS/model/grammar readiness checks; explicit asset preparation. Human report includes env/server and reachable-server index staleness. Exit 1 for unavailable required capabilities. |
| `export --out FILE` | stdout | Captures and validates a consistent format-2 JSONL snapshot, including history and retry receipts. File publication is atomic; vectors/indexes are rebuilt. |
| `export --url URL` | `GRAG_SERVER_URL` or unset | Explicit server override for online snapshot capture (bearer from `GRAG_API_TOKEN`). Without this flag, `--db` automatically uses its registered owner when available. `--server-db` selects a multi-db database. |
| `import --allow-legacy` | off | Explicitly accepts v1 exports without completion proof, history, receipts or internal ownership/lifecycle state. |
| `import FILE` | — | Restores into a new local `--db` file; validates completion, restores transactionally in staging, then checkpoints and verifies all contents after strict reopen before publication. Existing destinations are refused. |
| `init --client CLIENT` | `auto` | MCP client to configure: `claude`, `cursor`, `windsurf`, `zed`, or auto-detection. |
| `init --port PORT` | saved, otherwise derived per-project | Port written into generated MCP/shared-server configuration. New defaults derive from the database path (41000–49151); choose an explicit port if occupied. |
| `init --ingest` | off | Also runs `ingest-code` on the resolved project root immediately. |
| `init --remove` | off | Undoes init: removes the grag MCP entry and the CLAUDE.md block. |
| `init --url` | off | Writes direct HTTP URL transport instead of stdio plus auto-serve; the shared server must already be running. |
| `init --server-url URL` | unset | Registers a remote grag server instead of a local database: MCP config runs `grag mcp --server-url` (or, with `--url`, points at the server's `/mcp/` with a bearer header), referencing `${GRAG_API_TOKEN}` rather than storing it; CLAUDE.md documents the shared graph. `--server-db` selects a multi-db database. |
| `init --no-mcp` | off | Skips MCP client configuration. |
| `init --no-claude-md` | off | Skips the `CLAUDE.md` guidance block. |
| `init --dry-run` | off | Shows actual file diffs, including removal, without creating files or backups. |
| `relocate OLD_ROOT NEW_ROOT` | — | Reconciles a moved checkout's existing database and local registrations. Retains graph IDs and saved relationships; never moves or creates a database. |
| `relocate ... --dry-run` | off | Previews graph path/settings changes and client-file diffs without writing. |

Local CLI database selection is: an explicit `--db`/`--db-dir`, then environment
selectors, then `.grag/project.json`, then an unambiguous project-scoped legacy
registration, then `knowledge.lbdb` at the resolved project root (or current
directory outside a project). `init` saves this choice, allocating a unique
external database path for a new checkout. Python `GragConfig` retains its explicit
path/environment behavior; remote registrations do not create local mappings.
When the `embed-local` extra is installed, init also bakes
`GRAG_EMBED_PROVIDER=fastembed` into the MCP entry so the
auto-served daemon gets semantic search without any manual env setup. The CLI prevents
`--db` and `--db-dir` from appearing together. An explicit CLI selector also clears
the opposite selector inherited from the environment, so `--db` overrides
`GRAG_DB_DIR` and `--db-dir` overrides `GRAG_DB_PATH`.
