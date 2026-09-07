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

Choose that directory deliberately: scanning does not yet honor `.gitignore` or
stop at nested worktrees. If the whole project is a suitable scope, `grag init
--ingest` can combine setup and initial indexing. [Code ingestion](guides/code.md)
lists current language coverage and scope limits.

Use the MCP ingest tool while the server owns the database. Direct CLI ingest
does not yet forward to that server; [server ownership](operations/server.md)
explains when it is safe to use it.

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
grag status     # selected database, server address and log
grag doctor     # installation diagnostics; see installation guide for release-specific checks
grag stop       # clean shutdown of the selected managed server
```

The graph browser is served at the address printed by `status`. To connect a
second supported harness, run `init --client <client>` in the same checkout;
both registrations use the same mapping and shared server.
