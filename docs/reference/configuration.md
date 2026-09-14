# Configuration

The CLI starts from `GragConfig.from_env()`, then an explicitly supplied CLI
option wins where that command offers one. Constructing `GragConfig(...)` in Python
uses the values you pass plus the model defaults; it does **not** read the environment
unless you call `GragConfig.from_env()`.



<div class="grag-route" markdown>

Looking for a command flag? Use the [CLI reference](cli.md). For a common setup,
start with the [first-session guide](../getting-started.md).

</div>

## Database selection


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

## Environment variables

### Database and native execution

| Variable | Accepted values | Default | What it affects |
|---|---|---|---|
| `GRAG_DB_PATH` | Filesystem path | `knowledge.lbdb` | Database used in single-database mode. In multi-db mode, its filename identifies the preferred default database. Overridden by global CLI option `--db`. |
| `GRAG_DB_DIR` | Directory path | unset | Enables multi-database mode: short database names resolve to `<dir>/<name>.lbdb`. Overridden by global CLI option `--db-dir`. |
| `GRAG_BUFFER_POOL_MB` | Integer MiB | `256` | LadybugDB buffer-pool memory. Raise it for large imports/index builds; lower it to reduce resident-memory pressure. This is not the database file size. |
| `GRAG_STATEMENT_TIMEOUT_MS` | Integer 0–2147483647 | `30000` | Cooperative native execution limit per ordinary statement, in milliseconds. `0` disables it. Commit, rollback and checkpoint always finish without this deadline. Set it on the owning server and restart; another client's environment does not reconfigure a running owner. |
| `GRAG_WAL_AUTO_RECOVER` | `1`/`0` | `0` | Deprecated compatibility setting; no longer enables lossy recovery on startup. Use offline `grag --db <file> recover`, optionally with explicit `--allow-data-loss`. |

### Retrieval and freshness

| Variable | Accepted values | Default | What it affects |
|---|---|---|---|
| `GRAG_TOKEN_BUDGET` | Integer 256–32768 | `2000` | Estimated budget for the complete retrieval payload, including graph and metadata; request-level `token_budget` wins. Not a model-token guarantee. |
| `GRAG_SEARCH_LABEL_CAP` | Integer | `2` | Maximum fused search seeds contributed by one node label before other labels get a turn. Prevents large tables such as `Function` from crowding out `Decision`/`Concept`. Set `0` or a negative value to disable diversity capping and use pure fused rank order. |
| `GRAG_AUTO_REFRESH_CODE` | `1`/`0` | `1` | Serving reads schedule source-content/Git verification for registered code scopes and refresh changed scopes on the job thread. Plain folders work too; idle servers do not poll. |
| `GRAG_AUTO_REFRESH_INTERVAL_S` | Seconds | `30` | Minimum time between drift checks. |

### Servers and access

| Variable | Accepted values | Default | What it affects |
|---|---|---|---|
| `GRAG_SERVER_URL` | `https://host[:port]` | unset | Selects an already-running remote server for MCP, export and CLI graph commands (`remember`, `search`, `context`, `ingest`, `ingest-code`). MCP proxies stdio and replays its handshake after reconnecting; these clients do not open the remote `.lbdb` or start a local owner. |
| `GRAG_SERVER_DB` | Database name | unset | With `GRAG_SERVER_URL`: the `x-grag-db` header for a multi-db (`--db-dir`) server. |
| `GRAG_ALLOW_INSECURE_HTTP` | `1`/`0` | `0` | Permit a plain-`http://` `GRAG_SERVER_URL` to a non-loopback host (the bearer token then travels unencrypted). |
| `GRAG_API_TOKEN` | Non-empty bearer token | unset | Requires `Authorization: Bearer <token>` on REST routes except `/api/health`, and on HTTP MCP. A non-loopback standalone HTTP MCP bind is rejected when this is unset. The built-in UI stores a supplied token in that browser only. |
| `GRAG_CORS_ORIGINS` | Comma-separated origins | unset (no cross-origin access) | Adds allowed browser origins for separately hosted clients, for example `https://app.example.com,http://localhost:5173`. The built-in same-origin UI needs no entry; credentials remain disabled. |

### Optional embeddings

| Variable | Accepted values | Default | What it affects |
|---|---|---|---|
| `GRAG_VECTOR_CODEC` | `fp32`, `int8`, `binary`, `polar` | `fp32` | Storage/candidate-generation codec. `fp32` uses exact cosine scanning; compressed codecs scan compact codes and exactly rescore shortlisted fp32 vectors. Changes make existing vectors pending for automatic rebuilding. |
| `GRAG_POLAR_BITS_PER_DIM` | Float in `(0, 8]` | `1.0` | Approximate angular bits per vector dimension when `GRAG_VECTOR_CODEC=polar`. Higher values improve reconstruction at the cost of larger codes. Read directly by the polar codec. |
| `GRAG_MAX_EMBED_PER_SEARCH` | Non-negative integer | `256` | Maximum pending nodes embedded synchronously by one search when no background worker is running (`GRAG_EMBED_BACKGROUND=0`, or library use without a serving process). Remaining work is reported as `pending_embeddings`. |
| `GRAG_EMBED_BACKGROUND` | `1`/`0` | `1` | Serving processes embed stored-node text on a background worker per database. Query embedding remains part of semantic search. `0` makes node embedding inline too (search handles up to `GRAG_MAX_EMBED_PER_SEARCH`; ingest embeds its own writes). |
| `GRAG_EMBED_PROVIDER` | `fastembed` or `remote` | unset | Enables vector search. Unset means BM25/FTS-only retrieval. `fastembed` is local; `remote` sends embedding input to the configured OpenAI-compatible service. |
| `GRAG_EMBED_THREADS` | Integer | `min(4, cores)` | ONNX Runtime threads for the local embedder. `1` restores the conservative setting from before onnxruntime 1.29. |
| `GRAG_EMBED_QUERY_PREFIX` / `GRAG_EMBED_DOC_PREFIX` | String | by model family | Retrieval prefixes prepended to queries / node texts before embedding. Unset picks the family default (bge, nomic, e5, arctic, mxbai); empty string disables. Changes trigger automatic rebuilding. |
| `GRAG_EMBED_EXCLUDE_PROPS` | Comma list | `meta,path,heading_path,code_coverage,language,git_commit,git_branch,ingested_at` | STRING properties left out of embedding text (they stay in the FTS index). Changes invalidate affected vectors for rebuilding. |
| `GRAG_EMBED_MODEL` | Provider model name | `BAAI/bge-small-en-v1.5` | Embedding model identifier, used only when `GRAG_EMBED_PROVIDER` is set. Changing it invalidates/rebuilds affected embeddings lazily. |
| `GRAG_EMBED_DIM` | Positive integer | `384` | Embedding vector width. It must match the selected model's actual output dimension and the stored vector column. |
| `GRAG_EMBED_BASE_URL` | URL | unset | OpenAI-compatible endpoint root for the `remote` provider; required when using a remote embedding service. |
| `GRAG_EMBED_API_KEY_ENV` | Name of another environment variable | unset | Tells the remote provider which environment variable contains its bearer API key. This value is a variable **name**, not the secret itself. No authorization header is sent when unset. |

Embedding-specific variables are ignored until `GRAG_EMBED_PROVIDER` is set. For
local semantic search, install the optional dependency first:

```bash
pip install 'gragdb[embed-local]'
GRAG_EMBED_PROVIDER=fastembed grag --db knowledge.lbdb serve
```


## Python-only `GragConfig` options

These controls currently have no `GRAG_*` environment equivalent. Pass them when
embedding grag as a Python library; the CLI exposes the server-related subset shown
in the [CLI reference](cli.md).

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
`server_db`, `allow_insecure_http`, `auto_refresh_code`, `auto_refresh_interval_s`,
and `wal_auto_recover`. `EmbedderConfig` contains
`provider`, `model`, `dim`, `base_url`, and `api_key_env`, with the same meanings and
defaults listed above.

For Python embedding configuration, `text_props` can pin a list of STRING
properties per label. `exclude_props`, `query_prefix`, `document_prefix` and
`threads` correspond to the embedding text/prefix/thread environment settings.

### Native timeout behavior

Native timeout checks are cooperative: compilation, allocation and some operators
can run past the configured interval before checking it. This is not a hard wall-clock
limit on a tool call, lock wait, whole ingestion or shutdown. Query result/work limits
still apply. Python configuration rejects negative values, booleans and non-integers;
environment configuration rejects invalid integers before opening the database.

`grag doctor` reports the actual Ladybug backend and tests the selected local timeout.
The owning server's `/api/health` reports its `engine.backend`, version, effective
`statement_timeout_ms`, `completion_timeout_ms` and `writer_state`. Recreate a Python
Engine to apply configuration changes. Both pybind and Ladybug 0.20.4 (the pin in grag 0.10.0)
C-API backend use native limits; grag disables that version's erroneous Python
10 ms watchdog and synthetic range-query interrupt on its own connections only.



## CLI options

Command flags are grouped by task in the [CLI reference](cli.md).

### Everyday memory and retrieval

[Remember, inspect, retire, search and context flags →](cli.md#everyday-memory-and-retrieval)
