<!-- grag-managed skill reference: operations -->
# Setup, recovery and optional models

Use the existing MCP tools first. CLI `remember`, `search`, `context`, `ingest` and
`ingest-code` resolve the checkout and forward to its registered owner when present.
REST provides equivalent `/api/schema`, `/api/query`, `/api/search`, `/api/context`,
`/api/schema/define`, `/api/nodes/upsert`, `/api/edges/upsert` and ingestion routes.
Python uses `GragService(GragConfig(db_path=...))` and must close the service explicitly.

## First-use project mapping

A user-level skill makes the invocation available before a repo has any grag
files. One-time installation: `grag init --global-skill --client claude` (or
`cursor`, `codex`, `windsurf`, `zed`). This installs instructions and references
only; it does not bind every project to a global database. Reload the harness's
skills if needed. Duplicate-name precedence depends on the harness: current
Claude Code chooses a personal skill over a project copy. Update every installed
copy you intend to use rather than assuming the closest file wins.

For a bare skill invocation, resolve the current checkout and its existing
database selectors first. Use the user's explicit database or scope when given;
do not repurpose an unrelated user-scope MCP database. From the checkout, run:

```sh
grag init --client claude --ingest-if-empty
```

Use `--client cursor` in Cursor. In Codex or another harness whose MCP setup is
already managed separately, use `--client codex --no-mcp --no-claude-md` instead.
This still maps locally via the CLI. For Windsurf/Zed, whose init MCP registration
is user-scoped, prefer that CLI-only form when a registration serves another repo;
do not switch other sessions' database implicitly.

The command resolves the checkout, checks full table counts and provenance of
`GragSetup:connection`, and skips setup/indexing if other content already exists.
Empty schema tables do not count as a map. Unknown counts, ownership conflicts,
moved mappings and connection failures stop the check; report the actual error.
Do not retry by selecting a new database or forcing `--ingest`.

On `ingested`, inspect the returned counts, skips and parser coverage. Finish with
a brief mapping summary and one useful entrypoint's file/line citation. While MCP
is reconnecting, `grag search "<symbol>" --freshness require --json` can retrieve
one through the CLI. Keep ingestion counts and parser coverage in their existing
index metadata rather than duplicating them as authored memories. On `skipped`,
continue with the existing graph. On
`no_supported_code`, explain the reported exclusions/coverage; do not claim the
project is mapped or keep rescanning. Ingestion respects ignore rules and file
limits. Do not broaden ignores, install an embedder or invent a mandatory memory
schema just for startup. Read relevant docs separately if the user's task needs them.

New MCP registration may need a harness reconnect, but CLI indexing completes in
the current session. Report that distinction. If the installed command lacks
`--ingest-if-empty`, report that the newer version is needed instead of forcing
an unconditional scan. These flags are available from 0.10.0.

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

New in 0.10.0: `grag status --json` / `grag doctor --json` discover
runtime/source paths, checkout mapping, saved client registrations/scopes,
owner and skill copies without opening the graph or changing files. Doctor's
`ready` is installation readiness only. Cached owner observations are not a new
freshness check. Missing/moved/malformed configuration is not an empty graph.

For an explicit connection investigation, `grag doctor --verify-client
claude:project:grag` (or the exact Cursor/other ID shown in the report) launches
that saved command and reads its existing graph through MCP. It sends no setup
write, but startup can replay WAL and reads can refresh indexes. It requires a
matching explicit local database; URL/implicit/directory targets stay unverified.
Use `--db` to inspect another intended database. A passed probe is not a live GUI
session test. Inspect `verification` separately from installation `ready`.

Preview stale-launcher repairs with `init --dry-run --client CLIENT`; use
`relocate OLD NEW --dry-run` for moves. Doctor does not apply repairs. Renamed or
user/local registrations can need manual edits; init owns its `grag` entry only.
Linked configurations retain their links. Do not silently switch a broken client
to a fresh or partially recovered graph to make the check green.

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

New in 0.10.0: `index_inconsistent` identifies a persisted FTS failure,
not invalid Cypher. Preserve a recovery copy, export its readable records, and
import into a new database to rebuild derived indexes. Verify important memories,
the failed write, search and restart before adopting the new path. Keep the originals;
`reindex` rebuilds embeddings and does not repair FTS. Never silently replay a
failed mutation as if its outcome were known.

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
