# grag

**Local graph memory for AI agents.** Keep code structure, documents and decisions
in a project knowledge graph that your agent can query and update across sessions.

[Documentation](https://krokz.github.io/grag/) ·
[Getting started](https://krokz.github.io/grag/getting-started/) ·
[PyPI](https://pypi.org/project/gragdb/) ·
[DeepWiki](https://deepwiki.com/Krokz/grag)

grag combines an embedded [LadybugDB](https://ladybugdb.com/) database with MCP
tools, full-text search and optional vector retrieval. It returns source citations
and connected context under a configurable budget. The package is `gragdb`; the
command and Python import are `grag`.

## Start with your project

Python 3.10–3.14. Use a stable CLI installation so the launcher survives changes
to your project's virtual environment:

```bash
pipx install 'gragdb[code]'
cd your-project
grag init
```

`uv tool install 'gragdb[code]'` or `pip install 'gragdb[code]'` also works.
The `code` extra enables non-Python parsers; plain `gragdb` can index Python.

Restart your MCP client, then ask:

> Index this project's source directory with grag. Show me where the authentication
> functions are defined, with file and line citations.

> Remember why we chose this authentication approach, with its source, and link
> the decision to the relevant code.

`init` supports Claude Code, Cursor, Windsurf and Zed. Use `--client claude` or
`--client cursor` to select explicitly. It saves the checkout's database and port,
installs agent guidance, and configures a shared server that starts on first use.
`grag status` shows its address and log; `grag stop` shuts it down cleanly.

For a simple tree without nested worktrees or unwanted generated code,
`grag init --ingest` also indexes immediately. Scanning does not yet honor
`.gitignore`; see [scope and language coverage](https://krokz.github.io/grag/guides/code/).

**Windows:** the current native engine wheel may need OpenSSL DLLs installed
separately. Some agent harnesses also require starting the shared server in a
separate terminal. Read the [Windows installation notes](https://krokz.github.io/grag/installation/)
before your first ingest.

## What it helps with

- **Find code structure:** definitions, signatures and line ranges, with supported
  import/call relationships. Source bodies stay in your files.
- **Keep project memory:** agents save decisions, tasks and evidence, and can
  preserve correction history when updating them.
- **Connect documents and code:** index Markdown sections and link mentioned symbols.
- **Retrieve focused context:** BM25 search, optional embeddings, graph expansion,
  citations and explicit truncation. Budgets use a byte-based token estimate.
- **Share across harnesses:** Claude Code and Cursor can use one database through
  one owning server. New worktrees get separate databases by default.
- **Preserve continuity:** consistent backups retain authored history and retry
  receipts, with verified restore into a separate database.

Code coverage varies. Python, JS/TS and Go support conservative static relationships;
framework scripts and Java/C# overload identities also have explicit coverage.
See [language coverage](https://krokz.github.io/grag/guides/code/#language-coverage). An absent edge does not prove no relationship
exists. grag complements source search; useful answers and token savings depend on
the question, graph and harness.

## Local by default

The default install uses BM25 without an embedding service. Graph storage and
retrieval run locally. Native extensions and optional grammars/models can require
first-use downloads; prepare those assets before offline use. Your agent harness
may send retrieved context to its model provider.

[Local embeddings](https://krokz.github.io/grag/guides/embeddings/) are optional
through `gragdb[embed-local]` and ONNX Runtime. Installing that extra makes a later
`init` configure them automatically; model preparation costs time, disk and memory.
The optional remote provider sends embedding input to its configured endpoint.

## Go deeper

| Task | Guide |
|---|---|
| Install and verify a first session | [Getting started](https://krokz.github.io/grag/getting-started/) |
| Index code or documents | [Code](https://krokz.github.io/grag/guides/code/) · [Documents](https://krokz.github.io/grag/guides/documents/) |
| Save, revise and retrieve evidence | [Memory](https://krokz.github.io/grag/guides/memory/) · [Retrieval](https://krokz.github.io/grag/guides/retrieval/) |
| Connect multiple clients or move a checkout | [Servers](https://krokz.github.io/grag/operations/server/) · [Projects](https://krokz.github.io/grag/guides/projects/) |
| Diagnose startup, locks or recovery | [Troubleshooting](https://krokz.github.io/grag/operations/troubleshooting/) · [Recovery](https://krokz.github.io/grag/operations/recovery/) |
| Configure tools and resource limits | [MCP](https://krokz.github.io/grag/reference/mcp/) · [Configuration](https://krokz.github.io/grag/reference/configuration/) · [Limits](https://krokz.github.io/grag/reference/limits/) |
| Develop and measure grag | [Development](https://krokz.github.io/grag/development/) · [Contributing](https://github.com/Krokz/grag/blob/main/CONTRIBUTING.md) |

The maintained documentation lives in [`docs/`](https://github.com/Krokz/grag/tree/main/docs)
and publishes to GitHub Pages. DeepWiki is a supplementary, generated architecture
guide; check its displayed source commit and indexing date before relying on it.

[MIT license](https://github.com/Krokz/grag/blob/main/LICENSE).
