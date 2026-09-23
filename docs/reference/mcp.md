# MCP tools

The agent interface uses ten tools. Start with [init](../getting-started.md) to connect a harness.
{ .grag-lead }

## Tool catalog

Any MCP client gets these 10 tools:

| tool | purpose |
|---|---|
| `describe_schema` | compact schema: tables, property types, primary keys and directed endpoints. Optional full detail and revision-based reuse. Call before writing Cypher. |
| `define_schema` | create node/rel tables (LLM designs the graph for a domain); grag 0.10.0 validates reserved names and publishes the batch atomically |
| `upsert_nodes` / `upsert_edges` | atomic MERGE batches; optional retry IDs and revision checks; `_source` provenance automatic |
| `cypher_query` | read-only Cypher; errors come back with correction hints |
| `search_knowledge` | hybrid BM25 + vector seeds → RRF fusion → per-label diversity cap → k-hop expansion → cited, token-budgeted context |
| `get_context` | re-pack chosen node ids; page long STRING values with `text_property` |
| `ingest_code` | index code structure, including Repo/Module/Class/Function, Go Constant and TerraformModuleCall nodes, with language-specific relationships and coverage diagnostics; incremental graph updates; grag 0.10.0 reuses parses within an owner (`files_reused`), while resolving current dependencies; `background=true` returns a job id |
| `ingest_docs` | index server-local Markdown/text or JSON/JSONL records; `sections=false` uses flat chunks. grag 0.10.0 adds explicit `json_mode="document"` for ordinary JSON source text; the default remains records. See [document formats and limits](../guides/documents.md). |
| `job_status` | poll a background ingest by id |

## Responses and errors

Nested schema/upsert inputs advertise their required fields, property types and
revision guards. Expected failures set MCP `isError=true` with readable
`ERROR: ... HINT: ...` text and a JSON footer containing `code`, `error`, and
`hint`. The same envelope is available in `structuredContent`; REST uses the
same codes (and HTTP status codes). Validation errors identify nested fields.
grag 0.10.0 reports `index_inconsistent` for persisted FTS failures;
follow the [copy-based repair procedure](../operations/recovery.md#inconsistent-full-text-index)
before retrying the write.
Successful text responses carry their metadata once, without a duplicate
structured string.

## Reuse the schema view

MCP schema tools return the compact view by default: usable property types,
primary keys and relationship directions. `describe_schema(detail="full")`
also includes counts, sample keys and vector columns. REST/Python keep their
full default and accept the same detail option. Schema views are cached until
a writer statement invalidates them. Pass a previous `schema_revision` as
`if_revision` to omit a repeated view when `unchanged=true`; keep your cached
schema for that case. Revisions identify the requested view, including counts
and samples for full detail, and are separate from source freshness.


See [memory writes and history](../guides/memory.md), [task resumption](../guides/resume.md), [retrieval](../guides/retrieval.md), and [request limits](limits.md) for the full contracts.


## Tool loading in agent sessions

Every registered MCP tool adds its description and input schema to the agent's
context, and grag's server instructions and generated project guidance add more.
Measured with Claude Code 2.1.27x, registering grag's ten tools without deferral
added roughly 6,500 (Haiku) to 9,000 (Sonnet) tokens to the first request of each
session.

Claude Code can defer MCP tool schemas and load one only when the agent searches for
it (ToolSearch). Interactive sessions normally defer. Scripted `claude -p` runs that
pass an explicit `--tools` list must include `ToolSearch`, or every grag schema is
sent up front:

```bash
claude -p "..." --tools "Read,Glob,Grep,ToolSearch"
```

With `--debug`, the line `Dynamic tool loading: 0/10 deferred tools included`
confirms that no grag schema was loaded up front. In our tests, tool search did not
activate through a custom `ANTHROPIC_BASE_URL`, so all schemas were sent.

## Manual connection example


For ordinary setup, use [grag init](../getting-started.md). It records an absolute
database path and a shared-server port for your client. The manual example below
uses port 8471; every client sharing that database must use the same owner/port.

```bash
grag --db knowledge.lbdb mcp --auto-serve --port 8471
```

Cursor / `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "grag": {
      "command": "grag",
      "args": ["--db", "/absolute/path/knowledge.lbdb", "mcp", "--auto-serve", "--port", "8471"]
    }
  }
}
```
