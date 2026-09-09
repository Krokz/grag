# Your first session

## 1. Install

Use Python 3.10–3.14 and a stable command on PATH:

```bash
pipx install 'gragdb[code]'
cd your-project
```

You can also use `uv tool install` or `pip install`. Python-only indexing works
with plain `gragdb`. Start without embeddings; add them after checking that this
workflow helps. [Installation](installation.md) covers Windows native libraries,
first-use downloads and offline preparation.

## 2. Connect an agent

```bash
grag init
```

Init detects Claude Code, Cursor, Windsurf or Zed, saves a checkout mapping in
Git-ignored `.grag/project.json`, and configures an MCP connection and agent guidance.
Use `grag init --client cursor` or `--client claude` when choosing explicitly.
`grag init --dry-run` previews changes before applying.

Init also initializes the written MCP registration,
lists its tools, and writes/reads one non-searchable `GragSetup:connection` record.
It prints the registration, resolved command and selected database. Verification
failure returns a nonzero exit code while preserving the saved configuration.
Use `--no-verify` for offline configuration preparation; readiness remains unverified.
This checks the connection from your terminal environment. Reconnect the actual
harness too: its environment and permissions can differ.

The default new database is `~/.grag/<project-name>-<checkout-id>.lbdb`. Run commands
from anywhere inside the checkout to reuse that mapping. [Projects and relocation](guides/projects.md)
explains existing databases, worktrees, configuration backups and removal.

Restart or reconnect the MCP client. The configured proxy starts a shared local
server on first use. `grag status` prints its address and log location. On Windows,
follow the separate-terminal command if the harness prevents independent startup.

## 3. Index the intended source

Ask the agent:

> Use grag to index this project's source directory. Describe the schema and show
> one function's name, source path and line range.

Choose the intended source directory. Grag honors ignore rules
and skips nested worktrees and symlinks. `grag init --ingest` combines setup and
indexing; CLI ingestion uses the running owner when available.
[Code ingestion](guides/code.md) lists language coverage and scope options.

## 4. Save something worth retaining

> Remember a decision we made in this session, including why and its source.
> Reuse the existing schema, and link it to the relevant code if possible.

Open another session and ask for the decision and source. Confirm that the client
is selecting the same database. This verifies a useful write/read loop; `status`
alone only tells you about the server, not whether your harness can query it.

For questions about edited code, ask the agent to use `freshness="require"`.
Freshness verifies the registered code scope at the check time; it does not certify
the truth of authored memories. Read [freshness](guides/freshness.md) and
[memory corrections](guides/memory.md) when needed.

## Everyday commands

```bash
grag remember "Use a ten-minute cache" --id cache-policy
grag search "cache"
grag context Memory:cache-policy
grag status     # selected database, server address and log
grag doctor     # installation diagnostics; see installation guide for release-specific checks
grag stop       # clean shutdown of the selected managed server
```

The graph browser is served at the address printed by `status`. To connect a
second supported harness, run `init --client <client>` in the same checkout;
both registrations use the same mapping and shared server.
