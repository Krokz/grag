# Installation

**From PyPI** (ships the web UI):

```bash
pip install gragdb
```

Python 3.10–3.14. Python 3.13 is the shared Windows/macOS version in the CI matrix.
Linux, macOS, and Windows are all exercised in CI. For CLI + MCP use, prefer a
`pipx` / `uv tool` install: it puts a stable `grag` on PATH, so the MCP config
`grag init` writes keeps working when project virtualenvs come and go.

## Windows

**M16 development build:** Windows x64 grag wheels include the OpenSSL runtime
needed by LadybugDB. Ordinary wheel installs require no separate OpenSSL setup.
The libraries load from grag's own package directory. Source/editable Windows
installs need the [runtime build step](development.md#windows-source-builds).
Other platforms keep a small universal wheel without these DLLs.

**PyPI 0.8.0 and earlier do not include this fix.** A missing-OpenSSL error can
appear as `Could not find lbug C API shared library`. Prefer a release containing
the M16 fix when available, or build the development checkout; an older doctor
report saying “installed” does not prove native loading succeeds.

Human CLI output escapes characters a legacy terminal cannot represent. Saved
configuration files and exported JSONL retain UTF-8, including Unicode paths and
content. MCP protocol output remains separate from diagnostic messages.

Some Windows agent harnesses put MCP child processes in a Job Object that kills
them on disconnect. grag requires permission to start an independent shared
daemon; if Windows denies it, grag refuses the unsafe startup and prints a
`grag --db ... serve --with-mcp --port=...` command. Run that command in a separate
terminal, keep it open, and reconnect your agents. They share that server, and
disconnecting an agent leaves it running. Use `grag stop` for a clean shutdown.


## First-use downloads and offline use

The default install uses BM25 full-text search and does not configure an embedding
provider. The native engine can download its FTS extension on first use. Optional
language-pack grammars and local embedding models may also need downloads before
offline use. In the M16 development build:

```bash
grag doctor              # offline asset checks; no project DB opened
grag doctor --prepare    # explicitly allow missing asset downloads
grag doctor              # confirm cached assets now work
```

Use the same Python environment, operating-system user, and environment variables
as the MCP server. Doctor uses the current command's configuration; it does not
read another harness's MCP environment or change a running server's settings.
For local models, set `GRAG_EMBED_PROVIDER=fastembed` and the intended model/dimensions
before preparing. Set `FASTEMBED_CACHE_PATH` to a persistent writable cache if
the default temporary cache may be cleaned. Doctor never installs Python extras.

Preparation covers FTS, the optional legacy VECTOR extension, all grag-supported
grammars when the code extra is installed, and the configured local model. It
does not enable embeddings, ingest code, or send a remote embedding request.
Extension downloads use `extension.ladybugdb.com` and `~/.lbdb/extension`;
language-pack grammars use the package's release host and per-user cache, reported
by doctor. Models use their configured provider's model host and FastEmbed cache.
First use also logs preparation before a missing asset download. Cache/download
errors retain the asset name and underlying cause.

Local storage and retrieval do not send graph contents to a hosted retrieval
service. Your agent harness may send retrieved context to its model provider.
`GRAG_EMBED_PROVIDER=remote` also sends embedding input to the configured service.
See [embeddings](guides/embeddings.md) for the optional local model and its costs.

## What doctor verifies

The M16 doctor opens a temporary native database and runs Cypher, builds and
queries an FTS index, loads/parses each supported installed grammar, and runs
real inference for a configured local model. Separate child processes contain
native crashes and timeouts. Plain checks do not download assets. An installed
package or existing cache directory alone cannot produce a readiness success.

`grag doctor --json` returns structured install checks and `ready`. Exit code 1
means a required capability is unavailable; optional VECTOR absence does not
block normal FTS/exact-cosine use. Disabled extras and untested remote providers
are explicit. `--timeout <seconds>` sets each probe's deadline (30 seconds by
default, 300 with `--prepare`). Slow model preparation may need a longer deadline.

Doctor also reports the selected server, and code-index staleness when that server
is reachable. It never opens the project database itself, replays its WAL, repairs
corruption, or certifies the completeness of saved memories. Preparation does not
replace a damaged cached binary or reinstall a missing package. Retain the exact
error and see [troubleshooting](operations/troubleshooting.md).

## Engine compatibility

Without an embedder, everything works FTS-only (BM25 is native to the engine).

**LadybugDB compatibility.** This release pins LadybugDB 0.20.2. grag disables the
engine's cached-physical-plan fast path on every connection (`CALL
enable_cached_prepared_statement='none'`, the upstream kill switch for the
LadybugDB/ladybug#877 family of stale-re-execution bugs) and falls back to per-statement
eviction of the private prepared-statement cache on older runtimes. Do not downgrade an existing
database in place: a file opened by 0.20.x uses storage version 47 and cannot be
opened by 0.19.1 (storage version 43). A rollback requires exporting with the
newer compatible grag/Ladybug installation and importing into a fresh database.
