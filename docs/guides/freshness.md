# Code freshness

Serving processes check the
contents of supported files in each registered scope, plus Git HEAD when present.
Plain folders work too; changes do not depend on modification timestamps. Checks
and refreshes share the background job queue, and concurrent readers share one
verification job. Normal reads trigger a check at most every
`GRAG_AUTO_REFRESH_INTERVAL_S` seconds (default 30); failed work remains visible
and retries on subsequent reads with exponential backoff, from 1 to 300 seconds.
Waiting reads drive retries within their deadline. An idle server does not poll.

MCP `describe_schema`, `cypher_query`, `search_knowledge`, and `get_context` accept
`freshness` and `freshness_timeout_ms`. The corresponding Python service methods
and REST endpoints use the same policy; REST graph, schema, index-status, and
export GETs take it as query parameters. The UI provides these choices as well:

| `freshness` | Read behavior |
|---|---|
| `allow_stale` (default) | Schedule a due check and read the current graph without waiting for verification. |
| `wait` | Request a new check or join the active one; wait up to the deadline, then allow an unverified read with `timed_out: true`. |
| `require` | Wait as above, but reject the read if freshness cannot be verified. REST returns HTTP 503 with `code: "freshness_unavailable"`; MCP returns an error and freshness footer; Python raises `FreshnessError`. |

`freshness_timeout_ms` defaults to 5000 and accepts 0–60000. The deadline bounds
the verification wait, not execution of the graph query. Waiting modes bypass
the normal check interval, respect failure backoff, and leave shared work running
after a timeout. For example, call `search_knowledge(query="where is X defined",
freshness="require", freshness_timeout_ms=10000)` when an answer depends on edits.

Read responses include `freshness: {status, checked_at, timed_out}` in JSON or the
MCP/text footer; JSONL export carries it in the `X-Grag-Freshness` header. Status is
`fresh`, `checking`, `refreshing`, `stale`, `error`, `unknown`, or `disabled`.
Only `fresh` means the registered code scope was verified at that check. It does
not certify agent-authored memories, embeddings, or edits made after verification.
Export separately captures a consistent committed graph state; source freshness
does not imply that source files and graph capture form one atomic snapshot. Inspect
`GET /api/index/status` for each root's observed, pending, and last successful
generation, saved options, error, and retry delay. It uses the selected database
and normal authentication; anonymous `/api/health` exposes only the default
database's summary counters and freshness under `code_index`.

An explicit `ingest_code` saves its paths, `calls`, `max_file_kb`, `incremental`, and enclosing `root`
options for later refreshes and restarts. File-only scopes stay file-only; partial
ingests retain previously registered paths under the same root, and the latest
explicit options apply to that saved scope. A partial or failed ingest cannot
advance the last verified generation. Indexes created before this metadata existed
report `unknown`: explicitly run `ingest_code` once with the intended scope and
options to enroll them. Missing or relocated paths, parse/access failures, and
missing registered files stay unverified with diagnostics. Since 0.9.0,
newly ignored or size-excluded files are reconciled as deliberate
exclusions. `replace_scope=true` with an explicit root replaces its saved paths; an
empty list unregisters it. Relocation still requires explicit reconciliation. `GRAG_AUTO_REFRESH_CODE=0` disables checking; direct Python
services opt in with `service.enable_auto_refresh()`. A required-fresh read fails
when checking is disabled.
