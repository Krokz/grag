---
hide:
  - toc
  - navigation
---

<p class="grag-eyebrow">grag documentation</p>

# Project memory that carries forward

Keep code, documents and decisions in a local graph your agent can use across sessions.
{ .grag-lead }

[Set up your first project](getting-started.md){ .md-button .md-button--primary }
[Explore the guides](guides/index.md){ .md-button }

## What would you like to do?

<div class="grid cards" markdown>

-   **Get connected**

    Install grag, connect your agent and verify a first read and write.

    [Get started →](getting-started.md)

-   **Navigate a codebase**

    Index selected sources and find definitions, callers and source citations.

    [Index code →](guides/code.md)

-   **Keep useful memory**

    Save decisions, correct earlier notes and resume unfinished work.

    [Save and recall →](guides/memory.md)

-   **Find the right context**

    Retrieve relevant evidence within a budget and check its freshness.

    [Retrieve context →](guides/retrieval.md)

-   **Resolve a problem**

    Diagnose connection errors, missing libraries and database ownership.

    [Troubleshoot →](operations/troubleshooting.md)

-   **Look up an interface**

    Find MCP tools, CLI flags, environment settings and write contracts.

    [Open the reference →](reference/index.md)

</div>

<span id="keep-the-setup-small"></span>

## A small local setup

Start with one database per checkout and BM25 full-text search. Multiple agent
clients share one owning server. Embeddings and remote serving are optional.

<span id="one-graph-three-useful-kinds-of-context"></span>

| Bring to the graph | Use it for |
|---|---|
| Code structure | Definitions, signatures, line ranges and supported relationships |
| Documents | Source text, sections and links to code |
| Agent memory | Decisions, tasks and findings with their reasons and sources |

The graph can only retrieve what was indexed or saved. Your harness chooses when
to read or write, and may send retrieved context to its model provider.

[How grag fits together →](architecture.md)

<span id="documentation-scope"></span>

!!! info "Which version do these docs describe?"
    This guide describes **grag 0.11.0** and deploys from `main`.
    Features still in development are marked on the relevant page. This is one
    maintained guide rather than a separate copy for each release.

For an additional generated architecture view, visit
[DeepWiki](https://deepwiki.com/Krokz/grag). Check its index date and source commit;
its refresh schedule is independent of grag releases.
