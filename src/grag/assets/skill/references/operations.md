<!-- grag-managed skill reference: operations -->
# Setup, recovery and optional models

Use the existing MCP tools first. CLI `remember`, `search`, `context`, `ingest` and
`ingest-code` resolve the checkout and forward to its registered owner when present.
REST provides equivalent `/api/schema`, `/api/query`, `/api/search`, `/api/context`,
`/api/schema/define`, `/api/nodes/upsert`, `/api/edges/upsert` and ingestion routes.
Python uses `GragService(GragConfig(db_path=...))` and must close the service explicitly.

## Select the right database and owner

Local `grag init` stores a Git-ignored `.grag/project.json`. Explicit --db/--db-dir
wins over environment selectors, then the checkout mapping. Without a mapping,
unambiguous local Claude/Cursor registrations are respected; the project-root
knowledge.lbdb is the fallback. New worktrees/checkouts get separate identities.
To share intentionally, select the same absolute --db in each checkout's init.
Do not copy project mappings across worktrees.

One process owns each `.lbdb`. Use `grag serve --with-mcp` or init's shared auto-serve
registration for multiple Claude/Cursor sessions; do not launch competing direct
stdio owners. Init verifies the actual written MCP registration with a write/read;
--no-verify leaves readiness unverified. It also installs this skill and references.

Related source roots can share one graph. `--db-dir` hosts independent databases;
select one consistently with x-grag-db or REST ?db= (query wins). There are no cross-db
queries. A port/database identity mismatch needs the correct target or another port,
not stopping somebody else's server.

After a folder move, stop the owner and disconnect auto-starting clients. Use
`grag relocate /old/root /new/root --dry-run`, inspect, then apply without --dry-run.
It updates graph paths, saved scope and supported local launch configuration while
retaining graph IDs/memory; it does not move/create the database. Rerun after a
configuration publication failure to finish it. Restart clients and verify freshness.
User-scope registrations may need init again. A copied checkout should use init for
its own identity instead of pretending the original moved.

## Connection and storage failures

A reconnect failure alone does not identify a cause. Retry one transient tool error,
then inspect `grag --db <file> status` and its daemon log. Confirm database, process
and port. Use its shared owner for a lock error. Check paths/mapping after a move;
do not recreate or delete an unfamiliar DB. REST can help while MCP reconnects.
Report the actual error and attempted steps instead of looping retries.

`query_interrupted` is a native execution interruption, not a syntax error. Inspect
the owner's /api/health engine backend/statement_timeout_ms; doctor tests the local
runtime. Narrow the query/batch or set GRAG_STATEMENT_TIMEOUT_MS on the owner and
restart. Default 30000 ms; 0 disables it. This cooperative limit can overrun and is
not a whole-tool deadline. Commit, rollback and checkpoint run without that deadline.
`transaction_outcome_unknown` blocks further writes until reopen: restart the owner,
preserve DB/sidecars and replay the exact operation_id/payload. Without a receipt,
read stored state before retrying; the write may have committed.

For WAL/shadow replay failure, stop all users of that DB and pause supervisors.
`grag --db <file> recover` preserves originals/sidecars with checksums and tries strict
replay on a separate copy. If that fails, `--allow-data-loss` permits partial replay
on a new copy only with user authorization for possible loss. Committed memories may
be lost; the exact loss is unknown. Keep all preserved files and review the recovered
copy before adopting it. Never delete WAL/shadow files as a repair. `reindex` only
rebuilds embeddings in an openable database. Starts never silently choose lossy replay.

Shutdown rejects new work, cancels queued jobs and drains active workers. After its
grace period it may report a pending drain and retain the engine. A timeout does not
mean the process stopped; inspect its log/registration before opening a second owner.

## Verified backup and restore

```sh
grag --db knowledge.lbdb export -o backup.jsonl
grag --db new-copy.lbdb import backup.jsonl
```

Export uses a registered owner automatically or explicit --url/--server-db and bearer
from GRAG_API_TOKEN. Format 2 captures one committed state, pausing writes during
capture, then delivers a completed spool. The CLI validates checksum/count completion
before atomic file publication or stdout. Failed transfers preserve an existing backup.
Unfinished `.NAME.partial-*` files are staging, not completed backups.

Import needs a new local destination. It validates, restores transactionally in
`.grag-restore-*` staging, checkpoints, closes, strictly reopens and verifies all
contents before publishing without clobbering an existing file/sidecar. Abrupt death
can leave staging. Source input remains intact. Logical snapshots support grag's six
scalar types; unsupported native schemas fail explicitly instead of dropping data.

Format 2 preserves history, receipts, source ownership/lifecycle and saved ingestion
scope at the snapshot point. Later commits are absent. Node revisions and r2:
relationship revisions survive; old relationship receipts keep their old tokens,
so reread before a new guarded edit. Vectors/indexes rebuild. Source files, settings,
credentials and jobs are outside the snapshot. Paths still need verification or
relocation. Restore neither changes client mappings nor frees history/receipt capacity.
Legacy v1 requires --allow-legacy and lacks completion proof/history/receipts/ownership;
only string primary keys are recoverable. Treat it as a new retry target.

## Optional semantic search

Start with BM25. If semantic retrieval improves the actual task, install gragdb[embed-local]
and set GRAG_EMBED_PROVIDER=fastembed on the owning server. FastEmbed uses ONNX, not
PyTorch. Default model BAAI/bge-small-en-v1.5 is 384-dimensional. With matching model/
cache settings, `grag doctor --prepare` explicitly prepares missing assets; plain
`grag doctor` runs offline native/FTS/model/grammar checks without opening the project DB.
Use FASTEMBED_CACHE_PATH for persistent offline assets. A directory alone is not proof
of a usable model. Cached loading/inference still costs time and resident memory.

A search footer with vector=off means no embedder (expected BM25), error means a
configured embedder failed, and absent vector means its path succeeded.
pending_embeddings reports unfinished work only when configured; absence alone is
not proof that everything was embedded. Servers drain a background backlog; one-shot
operations can pay synchronous embedding cost. Model/prefix/text-policy/codec changes
invalidate vectors; reindex forces rebuilding. Dimension changes require migration.

Default fp32 uses exact cosine; other codecs are optional. Native HNSW is disabled
for the supported runtime because invalidation can crash it. Startup safely removes
old grag-owned indexes; external indexes need their owner's intervention. This does
not repair corrupt WAL. Do not add PyTorch/custom training or enable models by default
just to silence vector=off. Remote embedding is explicit and sends text off-machine.
