# Project memory that carries forward

grag keeps code structure, documents and decisions in a local graph. Your agent
can find a definition, retrieve the reason behind a decision, and save what it
learns for the next session.

**Install → connect your agent → index the intended sources → ask and remember.**

[Start with your project](getting-started.md){ .md-button .md-button--primary }
[Browse the tool reference](reference/mcp.md){ .md-button }

For the component boundaries, database ownership and data flows, see
[Architecture](architecture.md).

## One graph, three useful kinds of context

| Context | What grag stores | What you can ask |
|---|---|---|
| Code | Definitions, signatures, line ranges and supported relationships | “Where is this defined?” |
| Documents | Text, sections, chunks and code mentions | “Which documented rule applies here?” |
| Memory | Agent-authored facts, decisions, tasks and their sources | “Why did we choose this?” |

Retrieval starts with full-text search, optionally adds embeddings, and follows
graph relationships into cited context. It can return less material than reading
whole files; measure that benefit on your own questions. The graph cannot supply
knowledge that was never indexed or saved.

## Keep the setup small

The default is one local database per checkout and BM25 search. MCP clients share
one owning process; the CLI and Python library can also work directly while the
file has no other owner. Embeddings and remote serving are optional.

Start with [installation](installation.md) and [code coverage](guides/code.md).
Use [memory](guides/memory.md) for decisions, [retrieval](guides/retrieval.md) for
budgets and citations, and [troubleshooting](operations/troubleshooting.md) when a
session cannot connect.

## Documentation scope

These docs describe **grag 0.9.0**. They publish from the repository's `main`
branch; this is one maintained guide, not a separate documentation copy per release.
Limitations are called out where they affect a workflow, including Windows setup,
ingestion scope and language-specific edges.

[DeepWiki](https://deepwiki.com/Krokz/grag) provides an additional generated,
source-linked architecture view. Check its index date and source commit: its
refresh schedule is independent of this documentation and grag releases.
