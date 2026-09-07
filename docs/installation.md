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

The current LadybugDB wheel links OpenSSL 3 without bundling
it. If opening a database fails with `Could not find lbug C API shared library`
(a fallback error that can hide missing OpenSSL DLLs),
install [OpenSSL 3 for Win64](https://slproweb.com/products/Win32OpenSSL.html)
and copy `libssl-3-x64.dll` and `libcrypto-3-x64.dll` from its `bin\` folder
into the `ladybug.libs` directory next to the `ladybug` package in your Python
`site-packages`. Clean-install readiness remains a known limitation of this release.

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
offline use. Prepare and test the actual features in your intended environment
before disconnecting. An installed package alone does not establish readiness.

Local storage and retrieval do not send graph contents to a hosted retrieval
service. Your agent harness may send retrieved context to its model provider.
`GRAG_EMBED_PROVIDER=remote` also sends embedding input to the configured service.
See [embeddings](guides/embeddings.md) for the optional local model and its costs.

## Diagnostic limits

`grag doctor` reports package availability and the selected server/index state.
Its “core engine installed” check does **not** prove native DLL loading or a real
query succeeds. If startup fails, retain the exact error and daemon log; see
[troubleshooting](operations/troubleshooting.md).

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
