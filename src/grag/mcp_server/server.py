"""grag MCP server: the frozen tool contract (7 knowledge tools + ingest_code,
ingest_docs and job_status) over stdio or streamable-http.

Tool logic lives in plain module-level functions (a GragService is the first
argument) so tests exercise them without any MCP machinery. `create_server`
registers thin closures on an MCPServer (the mcp 2.0.0 API surface; pre-2.0
this class was called FastMCP) that resolve a per-call GragService from a
ServiceRegistry: the "x-grag-db" HTTP header names the database in multi-db
mode, stdio calls always get the default. `run` serves stdio, or the
streamable-http Starlette app via uvicorn.

Error contract: expected failures (GragError, pydantic ValidationError) are
RETURNED as "ERROR: <message>\\nHINT: <hint>" strings so the LLM receives the
failure as readable tool output and can self-correct. Unexpected exceptions
propagate.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import ipaddress
import json
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError
from starlette.responses import JSONResponse

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, GragError
from grag.core.types import (
    CodeIngestRequest,
    ContextRequest,
    DefineSchemaRequest,
    IngestRequest,
    MutationSummary,
    NodeTableSpec,
    QueryRequest,
    RelTableSpec,
    SearchRequest,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.registry import ServiceRegistry
from grag.service import GragService

__all__ = [
    "create_server",
    "cypher_query",
    "define_schema",
    "describe_schema",
    "get_context",
    "ingest_code",
    "ingest_docs",
    "job_status",
    "run",
    "search_knowledge",
    "upsert_edges",
    "upsert_nodes",
]

_F = TypeVar("_F", bound=Callable[..., str])

_COMPACT = (",", ":")  # json.dumps separators: tools return token-lean JSON


def _is_loopback_host(host: str) -> bool:
    """Return whether an explicit bind host is confined to this machine."""

    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


class _BearerAuthMiddleware:
    """Minimal ASGI bearer guard for the standalone MCP application."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            authorization = next(
                (
                    value
                    for key, value in scope.get("headers", [])
                    if key.lower() == b"authorization"
                ),
                b"",
            )
            if not hmac.compare_digest(authorization, self._expected):
                response = JSONResponse(
                    {"detail": "Missing or invalid bearer token"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _standalone_http_app(
    server: MCPServer, config: GragConfig, *, host: str, path: str
) -> Any:
    """Build a hardened standalone MCP ASGI app.

    Non-loopback exposure is rejected unless a bearer token is configured.
    The bind host is also passed into MCP's DNS-rebinding protection.
    """

    _validate_standalone_http_security(config, host)
    app = server.streamable_http_app(
        streamable_http_path=path, stateless_http=True, host=host
    )
    if config.api_token:
        return _BearerAuthMiddleware(app, config.api_token)
    return app


def _validate_standalone_http_security(config: GragConfig, host: str) -> None:
    if not _is_loopback_host(host) and not config.api_token:
        raise ConfigurationError(
            "Standalone HTTP MCP requires GRAG_API_TOKEN on a non-loopback host.",
            hint="Set GRAG_API_TOKEN or bind to 127.0.0.1/::1.",
        )


_INSTRUCTIONS = (
    "Always call search_knowledge before answering any question about this "
    "project's code, architecture, entities, or history — even when you think "
    "you already know. On an unfamiliar repository, call ingest_code on the "
    "paths first, then search. Read workflow: search_knowledge → get_context → "
    "cypher_query for exact structured reads. Write workflow: define_schema → "
    "upsert_nodes → upsert_edges. Record new facts you learn immediately with "
    "upsert_nodes / upsert_edges so the knowledge compounds across sessions. "
    "Embedding: search runs FTS-only by default. Semantic (vector) search — "
    "which significantly improves recall on natural-language queries — requires "
    "the server to be started with GRAG_EMBED_PROVIDER=fastembed (recommended: "
    "local, no API key, ONNX-based). If the user has not enabled it, suggest: "
    "pip install 'gragdb[embed-local]' then restart the server with "
    "GRAG_EMBED_PROVIDER=fastembed. When search_knowledge returns "
    "pending_embeddings > 0, nodes are still being embedded — the server's "
    "background worker drains the backlog on its own, so recall improves on "
    "later searches. When it returns \"vector\": \"off\", "
    "no embedder is configured on this server process — FTS-only is expected "
    "and pending_embeddings will never appear, so don't mistake that for "
    "\"fully embedded.\" \"vector\": \"error\" means an embedder is configured "
    "but failed for that call — say so rather than guessing why recall is FTS-only."
)


# --- error contract ---------------------------------------------------------------


def _format_grag_error(e: GragError) -> str:
    if e.hint:
        return f"ERROR: {e.message}\nHINT: {e.hint}"
    return f"ERROR: {e.message}"


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err["loc"]) or "<args>"
        parts.append(f"{loc}: {err['msg']}")
    msg = "Invalid arguments — " + "; ".join(parts)
    extra = len(e.errors()) - 5
    if extra > 0:
        msg += f"; +{extra} more"
    return (
        f"ERROR: {msg}\n"
        "HINT: Check the argument shapes against this tool's docstring; call "
        "describe_schema to confirm table names and primary keys."
    )


def _return_errors(fn: _F) -> _F:
    """Convert expected failures into readable tool output (never raise them)."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except GragError as e:
            return _format_grag_error(e)
        except ValidationError as e:
            return _format_validation_error(e)

    return inner  # type: ignore[return-value]


def _summary_json(summary: MutationSummary) -> str:
    return json.dumps(summary.model_dump(), ensure_ascii=False, separators=_COMPACT)


# --- plain tool functions (directly testable) -------------------------------------


@_return_errors
def describe_schema(service: GragService) -> str:
    """Return the current knowledge-graph schema as compact text: node tables
    with properties, primary keys, row counts and sample keys, plus
    relationship tables with their endpoint labels.

    Call this BEFORE writing any Cypher — cypher_query needs exact table and
    property names, and upsert keys are the table's primary key. Also call it
    after define_schema to see new tables (define_schema returns the fresh
    text too, so re-describing right away is optional). On an empty database
    the text is empty: define a schema first.
    """
    return service.describe_schema().text


@_return_errors
def define_schema(
    service: GragService,
    node_tables: list[dict],
    rel_tables: list[dict],
    if_not_exists: bool = True,
) -> str:
    """Create node and relationship tables. Use before the first upsert of a
    new entity or relationship kind. Idempotent: with if_not_exists=true
    (default) tables that already exist are left unchanged; set false to fail
    loudly on redefinition. Returns the fresh schema text (same shape as
    describe_schema).

    Args:
        node_tables: e.g. [{"name": "Person", "primary_key": "name",
            "properties": [{"name": "age", "type": "INT64"}],
            "searchable": true}].
            "name" is required; "primary_key" defaults to "id" and its column
            is created automatically; property "type" is one of STRING, INT64,
            DOUBLE, BOOL, DATE, TIMESTAMP (default STRING); "searchable"
            (default true) maintains a full-text index so search_knowledge can
            find these nodes.
        rel_tables: e.g. [{"name": "KNOWS", "from_label": "Person",
            "to_label": "Person", "properties": [{"name": "since",
            "type": "INT64"}]}]. "name", "from_label" and "to_label" are
            required, and both labels must be existing node tables (define
            them in the same or an earlier call).
    """
    req = DefineSchemaRequest(
        node_tables=[NodeTableSpec.model_validate(t) for t in node_tables],
        rel_tables=[RelTableSpec.model_validate(t) for t in rel_tables],
        if_not_exists=if_not_exists,
    )
    return service.define_schema(req).text


@_return_errors
def upsert_nodes(service: GragService, nodes: list[dict]) -> str:
    """Record facts you discover about the project. Create or update nodes.
    Identity is (label, key) where key is the
    table's primary-key value: an existing key merges properties, a new key
    creates the node, so re-upserts are idempotent. Properties not declared on
    the table are skipped with a warning (declare them via define_schema
    first); properties starting with "_" are grag-internal and always skipped.

    Args:
        nodes: e.g. [{"label": "Person", "key": "alice",
            "properties": {"age": 34}, "source": "notes/people.md"}].
            "label" must be an existing node table (see describe_schema);
            "key" is the table's primary-key value — the node's identity, so
            its canonical id becomes "Label:key" (e.g. "Person:alice");
            "properties" is a dict of declared column values; "source" is
            optional provenance (file, url, doc id) recorded automatically as
            the node's _source property.

    Returns JSON {"nodes": n, "edges": 0, "warnings": [...]} — always check
    "warnings" for skipped properties.
    """
    req = UpsertNodesRequest(nodes=[UpsertNode.model_validate(n) for n in nodes])
    return _summary_json(service.upsert_nodes(req))


@_return_errors
def upsert_edges(service: GragService, edges: list[dict]) -> str:
    """Record relationships between facts you discover. Create or update
    relationships between existing nodes. Both endpoint
    nodes must exist already (upsert_nodes first) and the direction must match
    the rel table's declared from/to labels. Identity is (type, from, to), so
    re-upserting the same edge merges properties idempotently.

    Args:
        edges: e.g. [{"type": "KNOWS", "from_label": "Person",
            "from_key": "alice", "to_label": "Person", "to_key": "bob",
            "properties": {"since": 2020}, "source": "notes/people.md"}].
            Endpoints are addressed by (label, primary-key value) — the same
            keys used in upsert_nodes. "source" is optional provenance
            recorded automatically as the edge's _source property.

    Returns JSON {"nodes": 0, "edges": n, "warnings": [...]} — always check
    "warnings" for skipped properties.
    """
    req = UpsertEdgesRequest(edges=[UpsertEdge.model_validate(e) for e in edges])
    return _summary_json(service.upsert_edges(req))


@_return_errors
def cypher_query(service: GragService, cypher: str, limit: int | None = None) -> str:
    """Run a READ-ONLY Cypher query (MATCH ... RETURN, aggregations, path
    patterns) and return compact JSON:
    {"columns": [...], "rows": [[...]], "row_count": n, "truncated": bool}.

    Call describe_schema first for exact table and property names. Write
    keywords (CREATE, MERGE, DELETE, SET, DROP, ...) are rejected — use
    define_schema, upsert_nodes and upsert_edges for writes. For fuzzy
    "what do we know about X" lookups prefer search_knowledge; use
    cypher_query for exact, structured reads.

    Args:
        cypher: the query text, e.g. "MATCH (p:Person)-[k:KNOWS]->(q:Person)
            RETURN p.name, q.name, k.since". Node/relationship values in rows
            come back as JSON objects (their "_ID"/"_LABEL" keys are
            grag-internal — ignore them; canonical "Label:key" ids come from
            search_knowledge / get_context).
        limit: max rows, clamped to the server's configured limits (default
            100). "truncated": true means more rows exist — narrow the query
            or raise limit.
    """
    resp = service.cypher_query(QueryRequest(cypher=cypher, limit=limit))
    payload = {
        "columns": resp.columns,
        "rows": resp.rows,
        "row_count": resp.row_count,
        "truncated": resp.truncated,
    }
    return json.dumps(payload, ensure_ascii=False, separators=_COMPACT, default=str)


@_return_errors
def search_knowledge(
    service: GragService,
    query: str,
    top_k: int = 8,
    hops: int = 1,
    labels: list[str] | None = None,
    token_budget: int | None = None,
) -> str:
    """Call this first for any question about what exists in the knowledge
    graph — even when you think you already know. Full-text seeds (plus vector
    seeds if an embedder is configured), k-hop graph expansion, packed into a
    token budget. This is the primary "what do we know about X?" tool — use it
    when you don't know exact node ids. Use get_context when you already have
    ids; use cypher_query for exact structured reads.

    Args:
        query: free-text query, e.g. "payment outage postmortem".
        top_k: max seed nodes (default 8).
        hops: graph expansion depth from each seed (default 1, server-clamped).
        labels: optional node-table allowlist for seeds, e.g. ["Doc",
            "Person"].
        token_budget: max tokens of packed context (server default when
            omitted).

    Returns the packed context (ready for grounding), a "---" separator, then
    a JSON footer {"seeds": [{"id": "Doc:42", "score": 0.016, "match":
    "fts"}, ...]}. Pass seed ids to get_context for focused follow-up
    expansion. When the footer includes "pending_embeddings": n, n nodes are
    still awaiting vector embedding — vector recall improves as later
    searches drain that backlog (ingest embeds its own writes in full). When
    the footer includes "index": "refreshing", the server noticed the indexed
    checkout changed (new commit or edited files) and is re-indexing it in the
    background: this answer reflects the graph before the change; ask again in
    a moment for anything that depends on the latest edits. When
    the footer includes "vector": "off", no embedder is configured for this
    server process — every seed is FTS-only and pending_embeddings will
    never appear (it's always 0), so don't read "no pending_embeddings" as
    "fully embedded." "vector": "error" means an embedder is configured but
    the vector path failed for this call (bad install or config) and
    silently fell back to FTS — worth checking server logs.
    """
    resp = service.search_knowledge(
        SearchRequest(
            query=query,
            top_k=top_k,
            hops=hops,
            labels=labels,
            token_budget=token_budget,
        )
    )
    payload: dict[str, Any] = {
        "seeds": [
            {"id": s.node.id, "score": round(s.score, 6), "match": s.match}
            for s in resp.seeds
        ]
    }
    if resp.pending_embeddings:
        payload["pending_embeddings"] = resp.pending_embeddings
    if resp.vector_status:
        payload["vector"] = resp.vector_status
    if resp.index_status:
        payload["index"] = resp.index_status
    footer = json.dumps(payload, ensure_ascii=False, separators=_COMPACT)
    if resp.context:
        return f"{resp.context}\n\n---\n{footer}"
    return footer


@_return_errors
def get_context(
    service: GragService,
    node_ids: list[str],
    hops: int = 1,
    token_budget: int | None = None,
) -> str:
    """Fetch token-budgeted context around specific nodes by canonical id. Use
    after search_knowledge (pass its seed ids) or with ids discovered via
    cypher_query.

    Args:
        node_ids: canonical ids "Label:key", e.g. ["Person:alice", "Doc:42"].
        hops: expansion depth around the nodes (default 1, server-clamped).
        token_budget: max tokens of packed context (server default when
            omitted).

    Returns the packed context string. Ids that don't resolve are skipped;
    unknown labels are an error (call describe_schema for the valid labels).
    """
    resp = service.get_context(
        ContextRequest(node_ids=node_ids, hops=hops, token_budget=token_budget)
    )
    return resp.context


@_return_errors
def ingest_code(
    service: GragService,
    paths: list[str],
    calls: bool = True,
    max_file_kb: int = 1024,
    background: bool = False,
) -> str:
    """Call this on any repository or file tree before answering questions about
    its code structure. Indexes the STRUCTURE of one or more code repositories into the graph:
    Repo/Module/Class/Function nodes (path, line range, signature, docstring —
    never source bodies) plus CONTAINS_*/IMPORTS/INHERITS/CALLS edges. Use it
    to answer "what calls X / what inherits from Y / what does module Z
    import" with cheap cypher_query instead of reading files. Re-running on
    the same tree preserves stable nodes and prunes removed files, symbols,
    and generated edges. Repo ids include a canonical-path hash, so same-named
    checkouts cannot collide. Parses Python via stdlib ast plus TypeScript/
    JavaScript/C#/Terraform/Go via tree-sitter (needs the
    optional extra: pip install "gragdb[code]"; CALLS/INHERITS edges are
    Python-only for now). Other code files are skipped with a warning.

    Terraform (.tf) `module` blocks also become TerraformModuleCall nodes
    (name, source, version), one per block, local or registry/git source
    alike — read straight off the .tf file, never retyped by hand. Prefer
    this over writing a module's version into a manually-authored node from
    a README or other doc: cypher_query it instead (e.g. MATCH
    (m:TerraformModuleCall) WHERE m.source CONTAINS '<name>' RETURN
    m.version) so the answer can't drift from what's actually pinned.

    Args:
        paths: repo directories (or single files) to walk, e.g. ["src"].
            Build artifacts and VCS dirs (.git, node_modules, dist, ...) are
            skipped automatically.
        calls: also record resolvable CALLS edges (default true).
        max_file_kb: skip files larger than this many KB (default 1024).
        background: queue the ingest on the server and return immediately
            with {"id": ..., "status": "queued"}; poll job_status(id) for the
            result. Use for large trees so this call does not block for
            minutes. Re-ingests are incremental either way: unchanged files
            are parsed for cross-file resolution but never rewritten.

    Returns compact JSON {"repos": n, "modules": n, "classes": n,
    "functions": n, "module_calls": n, "edges": n, "nodes_pruned": n,
    "edges_pruned": n, "files_parsed": n, "files_unchanged": n,
    "warnings": [...]} — always check "warnings" for skipped files.
    """
    req = CodeIngestRequest(paths=paths, calls=calls, max_file_kb=max_file_kb)
    if background:
        job = service.submit_ingest_code(req)
        return json.dumps(job.model_dump(), ensure_ascii=False, separators=_COMPACT)
    resp = service.ingest_code(req)
    return json.dumps(resp.model_dump(), ensure_ascii=False, separators=_COMPACT)


@_return_errors
def ingest_docs(
    service: GragService,
    paths: list[str],
    sections: bool = True,
    label: str = "Chunk",
    background: bool = False,
) -> str:
    """Index documents (.md/.txt/.json/.jsonl files or directories of them) on
    the server's filesystem into the graph. With sections=true (default) a
    Markdown file's heading hierarchy becomes Document -> Section nodes
    (SUBSECTION_OF / NEXT_SECTION between sections), each section's body is
    chunked under it (Chunk -IN_SECTION-> Section), and any code symbol the
    text names in backticks that exists in the code graph gets a
    MENTIONS_FUNCTION / MENTIONS_CLASS / MENTIONS_MODULE edge. Run ingest_code
    first so those links resolve. Use this for specs and design documents;
    sections=false is the flat chunk loader for loose notes.

    Re-running on the same files is an authoritative sync: current sections
    and chunks are merged, stale ones pruned. Section ids are stable
    ("<doc>#<heading/slug/path>"), so the empty IMPLEMENTS (Function ->
    Section) and IMPLEMENTS_CLASS tables are ready for you to record which
    code realises which part of the spec via upsert_edges.

    Args:
        paths: files or directories on the server, e.g. ["docs/algo-bible.md"].
        sections: heading-aware graph (default true) vs flat chunks.
        label: chunk node label (default "Chunk").
        background: queue the ingest and return {"id", "status": "queued"};
            poll job_status(id). Use for large documents.

    Returns compact JSON {"label", "nodes_created", "nodes_pruned",
    "documents", "sections", "code_links", "files_read", "warnings": [...]}.
    """
    from pathlib import Path

    from grag.ingest.loaders import load_paths

    documents, warnings, files_read = load_paths([Path(p) for p in paths])
    req = IngestRequest(documents=documents, label=label, sections=sections)
    if background:
        job = service.submit_ingest(req)
        payload = job.model_dump()
        payload["files_read"] = files_read
        payload["warnings"] = warnings
        return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)
    resp = service.ingest(req)
    payload = resp.model_dump()
    payload["files_read"] = files_read
    payload["warnings"] = warnings
    return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)


@_return_errors
def job_status(service: GragService, job_id: str) -> str:
    """Poll a background job started with ingest_code(background=true).

    Args:
        job_id: the "id" returned when the job was queued.

    Returns compact JSON {"id", "kind", "status": "queued"|"running"|"done"|
    "failed", "created_at", "started_at", "finished_at", "result", "error"}.
    "result" holds the ingest response once status is "done"; "error" the
    failure message when "failed". Unknown ids are an error (jobs live in
    memory for the serving process).
    """
    job = service.get_job(job_id)
    return json.dumps(job.model_dump(), ensure_ascii=False, separators=_COMPACT)


# --- MCP wiring ---------------------------------------------------------------------


def _doc(fn: Callable[..., Any]) -> str:
    return inspect.cleandoc(fn.__doc__ or "")


def _resolve_service(registry: ServiceRegistry, ctx: Context | None) -> GragService:
    """Pick the GragService for one tool call. The "x-grag-db" HTTP request
    header names the database in multi-db mode; stdio has no headers, so the
    registry default is used. ctx.headers raises ValueError outside a request
    (e.g. direct call_tool in tests), hence the broad guard."""
    db = None
    if ctx is not None:
        try:
            headers = ctx.headers
        except Exception:  # noqa: BLE001 — ctx.headers raises outside a request
            headers = None
        if headers:
            db = headers.get("x-grag-db")
    return registry.get(db)


def create_server(
    config: GragConfig, registry: ServiceRegistry | None = None
) -> MCPServer:
    """Build the MCP server: the frozen tools registered as thin closures that
    resolve their GragService per call (x-grag-db header on HTTP transports,
    the default database otherwise).

    Pass an existing `registry` to share one ServiceRegistry (one write conn
    per .lbdb) with a hosting app — e.g. when the REST server mounts the MCP
    endpoint so UI + MCP run in a single process against the same live file.
    When omitted, a fresh registry is created and owned by the returned server
    (run() closes it)."""
    if registry is None:
        registry = ServiceRegistry(config)
    server = MCPServer("grag", instructions=_INSTRUCTIONS)

    @server.tool(name="describe_schema", description=_doc(describe_schema))
    @_return_errors
    def describe_schema_tool(ctx: Context | None = None) -> str:
        return describe_schema(_resolve_service(registry, ctx))

    @server.tool(name="define_schema", description=_doc(define_schema))
    @_return_errors
    def define_schema_tool(
        node_tables: list[dict],
        rel_tables: list[dict],
        if_not_exists: bool = True,
        ctx: Context | None = None,
    ) -> str:
        return define_schema(
            _resolve_service(registry, ctx), node_tables, rel_tables, if_not_exists
        )

    @server.tool(name="upsert_nodes", description=_doc(upsert_nodes))
    @_return_errors
    def upsert_nodes_tool(nodes: list[dict], ctx: Context | None = None) -> str:
        return upsert_nodes(_resolve_service(registry, ctx), nodes)

    @server.tool(name="upsert_edges", description=_doc(upsert_edges))
    @_return_errors
    def upsert_edges_tool(edges: list[dict], ctx: Context | None = None) -> str:
        return upsert_edges(_resolve_service(registry, ctx), edges)

    @server.tool(name="cypher_query", description=_doc(cypher_query))
    @_return_errors
    def cypher_query_tool(
        cypher: str, limit: int | None = None, ctx: Context | None = None
    ) -> str:
        return cypher_query(_resolve_service(registry, ctx), cypher, limit)

    @server.tool(name="search_knowledge", description=_doc(search_knowledge))
    @_return_errors
    def search_knowledge_tool(
        query: str,
        top_k: int = 8,
        hops: int = 1,
        labels: list[str] | None = None,
        token_budget: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        return search_knowledge(
            _resolve_service(registry, ctx), query, top_k, hops, labels, token_budget
        )

    @server.tool(name="get_context", description=_doc(get_context))
    @_return_errors
    def get_context_tool(
        node_ids: list[str],
        hops: int = 1,
        token_budget: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        return get_context(
            _resolve_service(registry, ctx), node_ids, hops, token_budget
        )

    @server.tool(name="ingest_code", description=_doc(ingest_code))
    @_return_errors
    def ingest_code_tool(
        paths: list[str],
        calls: bool = True,
        max_file_kb: int = 1024,
        background: bool = False,
        ctx: Context | None = None,
    ) -> str:
        return ingest_code(
            _resolve_service(registry, ctx), paths, calls, max_file_kb, background
        )

    @server.tool(name="ingest_docs", description=_doc(ingest_docs))
    @_return_errors
    def ingest_docs_tool(
        paths: list[str],
        sections: bool = True,
        label: str = "Chunk",
        background: bool = False,
        ctx: Context | None = None,
    ) -> str:
        return ingest_docs(
            _resolve_service(registry, ctx), paths, sections, label, background
        )

    @server.tool(name="job_status", description=_doc(job_status))
    @_return_errors
    def job_status_tool(job_id: str, ctx: Context | None = None) -> str:
        return job_status(_resolve_service(registry, ctx), job_id)

    # Handles for lifecycle management (run closes the registry) and for
    # tests. grag_service is the default service for back-compat; in multi-db
    # mode with no determinable default there isn't one, so it is None.
    try:
        server.grag_service = registry.get()  # type: ignore[attr-defined]
    except GragError:
        server.grag_service = None  # type: ignore[attr-defined]
    server.grag_registry = registry  # type: ignore[attr-defined]
    return server


def run(
    config: GragConfig,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8472,
    path: str = "/mcp",
) -> None:
    """Serve the grag tool contract (blocks until shutdown). transport is
    "stdio" (default) or "streamable-http" (uvicorn serving the stateless
    Starlette app at `path` on host:port)."""
    if transport == "streamable-http":
        # Fail before opening or creating a database file.
        _validate_standalone_http_security(config, host)
    server = create_server(config)
    try:
        if transport == "streamable-http":
            import uvicorn

            app = _standalone_http_app(server, config, host=host, path=path)
            uvicorn.run(app, host=host, port=port)
        else:
            # CLI restricts transport to stdio|streamable-http; the literal
            # keeps the typed overloads happy.
            server.run(transport="stdio")
    finally:
        server.grag_registry.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    run(GragConfig.from_env())
