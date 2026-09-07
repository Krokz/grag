# Troubleshooting

Start with the exact client error, `grag status`, and the daemon log it prints.
Check the executable, database path and port in the client's MCP registration.
Another installation on PATH or another database can make a healthy server look
like the wrong project. Restart the selected server after an upgrade and reconnect
the client.

| Symptom | Check and next step |
|---|---|
| MCP fails to reconnect, but status shows a server | Verify the registration's launcher, database and port; exercise `describe_schema` through that client and inspect the log. Health alone does not verify its MCP connection. |
| Windows reports a missing native library | Follow the [OpenSSL instructions](../installation.md). Doctor's package-presence check does not verify native loading. |
| Windows refuses independent daemon startup | Run the exact `serve --with-mcp` command printed by grag in a separate terminal, keep it open, and reconnect. This avoids a harness killing the database owner on client disconnect. |
| `Could not set lock`, including Windows error 33 | Another process owns the file. Use that server's MCP/REST ingestion; for direct CLI work, disconnect auto-starting clients and stop the owner first. |
| `UnicodeEncodeError` in a Windows terminal | In PowerShell, try `$env:PYTHONIOENCODING = "utf-8"` before launching grag. The same setting can help when output is redirected. Native handling of legacy console encodings remains incomplete. |
| Unexpected duplicate modules or paths | Inspect the input roots and nested worktrees. Current scanning uses a fixed skip list, and each path argument defines a root. See [scope limits](../guides/code.md). |
| No TypeScript callers or missing Svelte code | These are [coverage limitations](../guides/code.md), not proof of absent code. Use source search. |
| Slow first semantic ingest/search | Check model preparation and pending embeddings. Serving workers run in the background; one-shot CLI ingests still embed synchronously. Start with BM25 when assessing usefulness. |
| Required-fresh read fails | Inspect `/api/index/status`, saved roots, errors and retry delays. Resolve moved paths or scope failures; a timeout does not cancel shared work. |
| Resource limit | Follow the returned resource/hint: narrow labels/scope, split batches, or wait for existing jobs. See [limits](../reference/limits.md). |
| Corrupted WAL / database cannot open | Stop its owners and preserve files. Follow [recovery](recovery.md); never delete WAL or shadow files to make it open. |

## Report a reproducible problem

Include the grag and Python versions, operating system, exact command or MCP tool,
selected runtime/database path (redacted if needed), error and relevant log lines.
For retrieval quality, include the question, expected source/lines, parameters and
actual response. Avoid sharing private database contents or credentials.

An empty result, unsupported parser edge and failed native open have different
causes. Reports with those details help keep fixes focused.
