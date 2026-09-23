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
returned with readable ERROR/HINT text plus a JSON error envelope. MCP
marks them isError=true and includes the same envelope in structuredContent.
Unexpected exceptions propagate to the SDK for logging and redaction.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import ipaddress
import json
import logging
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, GragError, validation_error_body
from grag.core.limits import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    check_size,
    json_bytes,
)
from grag.core.schema import schema_text
from grag.core.types import (
    CodeIngestRequest,
    ContextRequest,
    DefineSchemaRequest,
    FreshnessMode,
    MutationSummary,
    NodeTableSpec,
    QueryRequest,
    RelTableSpec,
    SchemaDetail,
    SearchRequest,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.registry import ServiceRegistry
from grag.request_limits import RequestLimitMiddleware
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
        return _BearerAuthMiddleware(RequestLimitMiddleware(app), config.api_token)
    return RequestLimitMiddleware(app)


def _validate_standalone_http_security(config: GragConfig, host: str) -> None:
    if not _is_loopback_host(host) and not config.api_token:
        raise ConfigurationError(
            "Standalone HTTP MCP requires GRAG_API_TOKEN on a non-loopback host.",
            hint="Set GRAG_API_TOKEN or bind to 127.0.0.1/::1.",
        )


_INSTRUCTIONS = (
    'Use connected MCP for graph reads/writes/ingestion; discover deferred tools. CLI is for '
    'setup/operations, explicit requests or unavailable/failed MCP. State fallback; keep the '
    'selected database. Empty results and validation errors are not connection failures. Use source search for '
    'code navigation; grag for saved decisions/findings and structural relationships. '
    'Use focused search with known labels or context for known IDs. Stop recall when claim, '
    'scope, source and qualifications suffice; read again for a specific evidence gap, '
    'current-code check or edit guard. Off-topic hits need source inspection or corrected '
    'scope. Search needs no schema preflight; '
    'projected Cypher needs familiar schema. Repo names in query text are not scope filters. '
    'Capture step: when the user states a decision or you establish a reusable finding, save it '
    'before finishing unless the request is read-only. One search with explicit memory labels (an '
    'explicit zero in label_hits means no match, unless excluded_evidence is above zero — lifecycle-hidden matches; check evidence="all" before saving; a label in unknown_labels does not exist in this graph; do not repeat), then a guarded upsert_nodes with a scalar key, a source citing '
    'discussion and code, and a body keeping claim, scope, qualifications and unchosen proposals. '
    'Use only listed relationship types or omit edges. '
    'Within authorized memory work, correct verified stale findings using revision guards '
    'and history without asking again; preserve user decisions and read-only scope. Only '
    'freshness.status=fresh verifies the registered code scope at checked_at, not '
    'memories/embeddings or later edits. require errors if verification fails; wait can '
    'return unverified at its deadline. freshness_timeout_ms (0..60000, default 5000) bounds '
    'verification, not query execution. Legacy indexes need explicit ingest_code enrollment. '
    'Inspect evidence qualifiers, omissions and parser '
    'coverage; absence is not completeness. Reuse records; retry lost writes with the exact '
    'operation ID/payload. BM25 works without embeddings. Never delete WAL/shadow '
    'files to repair a database.'
)


# --- error contract ---------------------------------------------------------------


class _ErrorText(str):
    """Readable direct-call compatibility with a typed MCP error payload."""

    payload: dict

    def __new__(cls, payload: dict):
        readable = f"ERROR: {payload['error']}"
        if payload.get("hint"):
            readable += f"\nHINT: {payload['hint']}"
        readable += f"\nCODE: {payload['code']}"
        readable += "\n---\n" + json.dumps(payload, ensure_ascii=False, separators=_COMPACT)
        obj = super().__new__(cls, readable)
        obj.payload = payload
        return obj


def _format_grag_error(e: GragError) -> str:
    return _ErrorText(e.to_dict())


def _format_validation_error(e: ValidationError) -> str:
    return _ErrorText(validation_error_body(e))


def _error_result(payload: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=_ErrorText(payload))],
        structured_content=payload, is_error=True,
    )


def _mcp_result(fn: _F) -> Callable[..., CallToolResult]:
    """Do not make agents infer failure from an apparently successful string."""
    safe_fn = _return_errors(fn)

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> CallToolResult:
        result = safe_fn(*args, **kwargs)
        if isinstance(result, _ErrorText):
            return _error_result(result.payload)
        # No duplicate structured string: text already carries success metadata.
        return CallToolResult(content=[TextContent(type="text", text=result)])

    return wrapped


class _GragMCPServer(MCPServer):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        try:
            json_bytes(arguments, MAX_REQUEST_BYTES, "request_bytes")
            return await super().call_tool(name, arguments, context)
        except GragError as exc:
            return _error_result(exc.to_dict())
        except ToolError as exc:
            # Typed arguments are validated by the SDK before the tool body.
            # Use the public call boundary so HTTP and stdio see the same error.
            if not isinstance(exc, UnexpectedToolError) and isinstance(exc.__cause__, ValidationError):
                return _error_result(validation_error_body(exc.__cause__))
            if isinstance(exc, UnexpectedToolError):
                logging.getLogger(__name__).exception("MCP tool %r failed", name)
                return _error_result({"code": "internal_error", "error": "Internal server error.", "hint": None})
            return _error_result({"code": "tool_error", "error": str(exc), "hint": None})


def _return_errors(fn: _F) -> _F:
    """Convert expected failures into readable tool output (never raise them)."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> str:
        try:
            result = fn(*args, **kwargs)
            check_size("response_bytes", len(result.encode("utf-8")), MAX_RESPONSE_BYTES)
            return result
        except GragError as e:
            return _format_grag_error(e)
        except ValidationError as e:
            return _format_validation_error(e)

    return inner  # type: ignore[return-value]


def _summary_json(summary: MutationSummary) -> str:
    payload = summary.model_dump(exclude={"operation_id", "replayed", "revisions", "history"})
    if summary.operation_id is not None:
        payload.update(operation_id=summary.operation_id, replayed=summary.replayed)
    if summary.revisions:
        payload["revisions"] = summary.revisions
    if summary.history:
        payload["history"] = summary.history
    return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)


# --- plain tool functions (directly testable) -------------------------------------


@_return_errors
def describe_schema(
    service: GragService, freshness: FreshnessMode = "allow_stale", freshness_timeout_ms: int = 5000,
    detail: SchemaDetail = "compact", if_revision: str | None = None,
) -> str:
    """Read schema before Cypher or schema changes. Compact output gives labels, properties,
    primary keys and directed relationship endpoints. detail="full" adds counts/samples.
    Cache schema_revision; if_revision returns unchanged=true only for the same detail.
    Graph freshness is separate from schema revision.
    """
    doc = service.describe_schema(freshness=freshness, freshness_timeout_ms=freshness_timeout_ms, detail=detail, if_revision=if_revision)
    return schema_text(doc)


@_return_errors
def define_schema(
    service: GragService,
    node_tables: Sequence[NodeTableSpec | dict],
    rel_tables: Sequence[RelTableSpec | dict],
    if_not_exists: bool = True,
    allow_similar: bool = False,
) -> str:
    """Create/reuse node and directed relationship tables; inspect existing schema first. Node
    keys default to STRING id. Declare property types and relationship endpoints. Names must
    be unquoted ASCII identifiers; near-duplicates require allow_similar=true. if_not_exists
    preserves existing tables. Returns compact schema.
    """
    req = DefineSchemaRequest(
        node_tables=[NodeTableSpec.model_validate(t) for t in node_tables],
        rel_tables=[RelTableSpec.model_validate(t) for t in rel_tables],
        if_not_exists=if_not_exists,
        allow_similar=allow_similar,
    )
    doc = service.define_schema(req, detail="compact")
    return schema_text(doc)


@_return_errors
def upsert_nodes(service: GragService, nodes: Sequence[UpsertNode | dict], edges: Sequence[UpsertEdge | dict] | None = None, operation_id: str | None = None) -> str:
    """Atomically save nodes and optional edges using existing labels/properties; describe_schema
    first if unfamiliar. Each node has label, key, properties and source: source belongs beside
    properties, never inside it or as _source. Repair skipped-property warnings. Omission preserves
    values; null clears. Limit: 1000 nodes/edges, 2 MiB. Guard edits with expected_revision from a
    whole-entity read (search_knowledge and get_context return it as _revision), or 'absent' for
    create-only. Lost response: retry exact payload/operation_id.
    evidence={} starts history; adopting existing nodes needs a guard. Caller review is not
    verification. Returns counts, warnings, revisions and a per-node history disposition:
    created, recorded (prior version retained), or not_recorded (overwritten without history —
    a guard alone does not retain the prior body). See the skill's memory reference.
    """
    req = UpsertNodesRequest(nodes=[UpsertNode.model_validate(n) for n in nodes], edges=[UpsertEdge.model_validate(e) for e in edges or []], operation_id=operation_id)
    return _summary_json(service.upsert_nodes(req))


@_return_errors
def upsert_edges(service: GragService, edges: Sequence[UpsertEdge | dict], operation_id: str | None = None) -> str:
    """Atomically save directed relationships between existing endpoints; upsert_nodes can create
    endpoints and edges together. Supply source; inspect warnings. Limit: 1000 edges, 2 MiB.
    Use current expected_revision (or 'absent') for guards; legacy tokens need a reread. Retry
    lost responses with the exact operation_id and payload. Returns counts, warnings and
    revisions.
    """
    req = UpsertEdgesRequest(edges=[UpsertEdge.model_validate(e) for e in edges], operation_id=operation_id)
    return _summary_json(service.upsert_edges(req))


@_return_errors
def cypher_query(
    service: GragService, cypher: str, limit: int | None = None,
    freshness: FreshnessMode = "allow_stale", freshness_timeout_ms: int = 5000,
) -> str:
    """Run read-only Cypher with compact JSON columns/rows/row_count/truncated/freshness.
    Read describe_schema first; project only needed fields for exact lookups or counts.
    Use search_knowledge for topics in saved knowledge. This tool accepts no writes.
    For task resumption, filter declared scope/status and use explicit priorities;
    task IDs, mission numbers and relevance scores do not establish priority.
    Whole nodes/relationships include computed _revision and omit vectors/null columns;
    _revision is not a stored Cypher property. Explicit property/map projections remain
    exact, including nulls and vectors. Return endpoints with relationships for canonical
    subgraph IDs; native _ID/_SRC/_DST are storage identities, not durable IDs.
    Default limit=100, clamped by server policy; truncated=true means more rows exist.
    An empty result is limited to the indexed scope and available parser coverage.
    """
    resp = service.cypher_query(QueryRequest(
        cypher=cypher, limit=limit, freshness=freshness, freshness_timeout_ms=freshness_timeout_ms,
    ))
    # Match REST's JSON conversion (especially timestamps and nested values).
    payload = resp.model_dump(mode="json", exclude={"subgraph"})
    from grag.core.serialize import compact_graph_values
    payload["rows"] = compact_graph_values(payload["rows"])
    return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)


@_return_errors
def search_knowledge(
    service: GragService,
    query: str,
    top_k: int = 8,
    hops: int = 1,
    labels: list[str] | None = None,
    token_budget: int | None = None,
    freshness: FreshnessMode = "allow_stale",
    freshness_timeout_ms: int = 5000,
    evidence: Literal["current", "all"] = "current",
) -> str:
    """Discover cited context with a focused topic; no schema preflight required. BM25 plus
    optional vectors, then graph expansion. Narrow labels only when known; repo names in
    query text are not scope filters. Use projected Cypher for exact names/path filters.
    For an initial lead, try top_k=4, hops=0; expand only for needed evidence. Off-topic hits
    need a better query/scope or source inspection, not more seeds. Defaults stay 8/1.
    Resume work via exact status/scope/priority queries; relevance is not task order.
    Inspect the JSON footer: freshness, evidence_policy, truncated and omission counts.
    Current evidence excludes superseded/retracted/expired/disputed/obsolete nodes;
    evidence="all" includes them with qualifiers. Unreviewed evidence is not certified.
    excluded_evidence counts hidden matches from a bounded lexical recount plus encountered
    exclusions. It can miss vector-only hidden matches; zero does not prove absence.
    If seeds look wrong or empty and the count is above zero, retry with evidence="all".
    _source_changed lists files a tracked record cites that changed since it was saved;
    only cited files are checked.
    Whole properties may be omitted to fit token_budget. text_excerpts are partial slices;
    follow their node/property/offset/sha256 using get_context paging before claiming
    complete evidence. Empty/complete output never proves exhaustive graph coverage.
    BM25 works without an embedder; vector="off" is expected then. vector="error" means
    configured embeddings failed; pending_embeddings reports unfinished embedding work.
    Budgets are UTF-8/4 estimates (256..32768) bounding this text reply, not tokenizer counts.
    """
    resp = service.search_knowledge(
        SearchRequest(
            query=query,
            top_k=top_k,
            hops=hops,
            labels=labels,
            token_budget=token_budget,
            freshness=freshness, freshness_timeout_ms=freshness_timeout_ms,
            evidence=evidence,
        ),
        surface="mcp",
    )
    from grag.retrieval.packing import mcp_retrieval_text

    return mcp_retrieval_text(resp)


@_return_errors
def get_context(
    service: GragService,
    node_ids: list[str],
    hops: int = 1,
    token_budget: int | None = None,
    text_property: str | None = None,
    text_offset: int = 0,
    text_sha256: str | None = None,
    freshness: FreshnessMode = "allow_stale",
    freshness_timeout_ms: int = 5000,
    evidence: Literal["current", "all"] = "current",
    history: bool = False,
    history_before: int | None = None,
    revision: int | None = None,
) -> str:
    """Retrieve cited context for known Label:key IDs, expanding up to hops within token_budget.
    Inspect freshness, evidence_policy, truncated and omission counters; absent properties
    may be omitted for space. evidence="current" filters obsolete/retracted/disputed/expired
    nodes; evidence="all" includes qualified old evidence. It does not certify truth.
    For a long STRING, pass exactly one ID and text_property; page with text_offset and
    text_sha256 from text_page until next_offset=null. If changed, restart at zero without
    the hash. Page mode skips expansion; a last suffix may still report truncated=true.
    For tracked memory, history=true lists up to 20 revisions; continue using history_before.
    revision=<sequence> retrieves one saved snapshot and can combine with text_property.
    History needs one ID, starts at adoption, and does not reconstruct past relationships.
    Packed nodes carry their _revision guard token; pass it as expected_revision to correct
    a record. _source_changed lists cited files changed since a tracked record was saved. Page mode (text_property) does not project _revision; read without it first.
    Budgets are UTF-8/4 estimates (256..32768) bounding this text reply; use Cypher for exact structured projections.
    """
    resp = service.get_context(
        ContextRequest(
            node_ids=node_ids, hops=hops, token_budget=token_budget,
            text_property=text_property, text_offset=text_offset, text_sha256=text_sha256,
            freshness=freshness, freshness_timeout_ms=freshness_timeout_ms,
            evidence=evidence, history=history, history_before=history_before, revision=revision,
        ),
        surface="mcp",
    )
    from grag.retrieval.packing import mcp_retrieval_text

    return mcp_retrieval_text(resp)


@_return_errors
def ingest_code(
    service: GragService,
    paths: list[str],
    calls: bool = True,
    max_file_kb: int = 1024,
    background: bool = False,
    root: str | None = None,
    replace_scope: bool = False,
) -> str:
    """Index server-local code structure and file/line pointers. Python is built in; other
    supported languages need gragdb[code]. Edges are partial static analysis: inspect
    Module.code_coverage; missing edges do not prove absence. Paths honor ignores and skip
    symlinks/nested repos. Registered paths accumulate; replace_scope=true requires root, with
    paths=[] to unregister. Re-ingest reconciles generated content while retaining authored
    links; inspect warnings/obsolete states. Use background=true for large scans, then poll
    job_status. Scope, parser coverage and saved options: skill ingestion reference.
    """
    req = CodeIngestRequest(paths=paths, calls=calls, max_file_kb=max_file_kb, root=root, replace_scope=replace_scope)
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
    json_mode: Literal["records", "document"] = "records",
) -> str:
    """Index server-local Markdown/text/JSON/JSONL with provenance. Default JSON expects document
    records ({text, source?, metadata?}); use json_mode='document' for ordinary .json files.
    JSONL stays records. sections=true preserves Markdown headings; ingest code first for
    symbol links. Successful scans reconcile generated content; authored links can retain
    obsolete nodes. Inspect warnings. Document edits need re-ingestion. Use background=true
    for large input, then poll job_status. Formats, limits and scope: skill ingestion
    reference.
    """
    from pathlib import Path

    from grag.ingest.loaders import load_request

    req, warnings, files_read = load_request([Path(p) for p in paths], label=label, sections=sections, json_mode=json_mode)
    if background:
        job = service.submit_ingest(req)
        payload = job.model_dump()
        payload["files_read"] = files_read
        payload["json_mode"] = json_mode
        payload["warnings"] = warnings
        return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)
    resp = service.ingest(req)
    payload = resp.model_dump()
    payload["files_read"] = files_read
    payload["json_mode"] = json_mode
    payload["warnings"] = [*warnings, *resp.warnings]
    return json.dumps(payload, ensure_ascii=False, separators=_COMPACT)


@_return_errors
def job_status(service: GragService, job_id: str) -> str:
    """Poll a background ingestion job. queued/running are unfinished; done includes
    result/warnings; failed includes error. Jobs are process-local; cancelled/lost jobs may
    need resubmission.
    """
    job = service.get_job(job_id)
    return json.dumps(job.model_dump(), ensure_ascii=False, separators=_COMPACT)


# --- MCP wiring ---------------------------------------------------------------------


def _doc(fn: Callable[..., Any]) -> str:
    doc = inspect.cleandoc(fn.__doc__ or "")
    if fn.__name__ in {"describe_schema", "cypher_query", "search_knowledge", "get_context"}:
        doc += "\nUse freshness='require' for current code; inspect freshness.status. Timeout/error is not verification."
    return doc


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
    from grag import __version__

    server = _GragMCPServer("grag", instructions=_INSTRUCTIONS, version=__version__)

    @server.tool(name="describe_schema", structured_output=False, description=_doc(describe_schema))
    @_mcp_result
    def describe_schema_tool(
        freshness: FreshnessMode = "allow_stale", freshness_timeout_ms: int = 5000,
        detail: SchemaDetail = "compact", if_revision: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        return describe_schema(_resolve_service(registry, ctx), freshness, freshness_timeout_ms, detail, if_revision)

    @server.tool(name="define_schema", structured_output=False, description=_doc(define_schema))
    @_mcp_result
    def define_schema_tool(
        node_tables: list[NodeTableSpec],
        rel_tables: list[RelTableSpec],
        if_not_exists: bool = True,
        allow_similar: bool = False,
        ctx: Context | None = None,
    ) -> str:
        return define_schema(
            _resolve_service(registry, ctx),
            node_tables,
            rel_tables,
            if_not_exists,
            allow_similar,
        )

    @server.tool(name="upsert_nodes", structured_output=False, description=_doc(upsert_nodes))
    @_mcp_result
    def upsert_nodes_tool(nodes: list[UpsertNode], edges: list[UpsertEdge] | None = None, operation_id: str | None = None, ctx: Context | None = None) -> str:
        return upsert_nodes(_resolve_service(registry, ctx), nodes, edges, operation_id)

    @server.tool(name="upsert_edges", structured_output=False, description=_doc(upsert_edges))
    @_mcp_result
    def upsert_edges_tool(edges: list[UpsertEdge], operation_id: str | None = None, ctx: Context | None = None) -> str:
        return upsert_edges(_resolve_service(registry, ctx), edges, operation_id)

    @server.tool(name="cypher_query", structured_output=False, description=_doc(cypher_query))
    @_mcp_result
    def cypher_query_tool(
        cypher: Annotated[str, Field(max_length=65_536)], limit: int | None = None,
        freshness: FreshnessMode = "allow_stale", freshness_timeout_ms: int = 5000,
        ctx: Context | None = None,
    ) -> str:
        return cypher_query(_resolve_service(registry, ctx), cypher, limit, freshness, freshness_timeout_ms)

    @server.tool(name="search_knowledge", structured_output=False, description=_doc(search_knowledge))
    @_mcp_result
    def search_knowledge_tool(
        query: Annotated[str, Field(max_length=8192)],
        top_k: Annotated[int, Field(ge=1, le=64)] = 8,
        hops: int = 1,
        labels: Annotated[list[str], Field(max_length=64)] | None = None,
        token_budget: Annotated[int, Field(ge=256, le=32_768)] | None = None,
        freshness: FreshnessMode = "allow_stale",
        freshness_timeout_ms: int = 5000,
        evidence: Literal["current", "all"] = "current",
        ctx: Context | None = None,
    ) -> str:
        return search_knowledge(
            _resolve_service(registry, ctx), query, top_k, hops, labels, token_budget,
            freshness, freshness_timeout_ms, evidence,
        )

    @server.tool(name="get_context", structured_output=False, description=_doc(get_context))
    @_mcp_result
    def get_context_tool(
        node_ids: Annotated[list[str], Field(max_length=64)],
        hops: int = 1,
        token_budget: Annotated[int, Field(ge=256, le=32_768)] | None = None,
        text_property: str | None = None,
        text_offset: int = 0,
        text_sha256: str | None = None,
        freshness: FreshnessMode = "allow_stale",
        freshness_timeout_ms: int = 5000,
        evidence: Literal["current", "all"] = "current",
        history: bool = False,
        history_before: int | None = None,
        revision: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        return get_context(
            _resolve_service(registry, ctx), node_ids, hops, token_budget,
            text_property, text_offset, text_sha256, freshness, freshness_timeout_ms,
            evidence, history, history_before, revision,
        )

    @server.tool(name="ingest_code", structured_output=False, description=_doc(ingest_code))
    @_mcp_result
    def ingest_code_tool(
        paths: Annotated[list[str], Field(max_length=64)],
        calls: bool = True,
        max_file_kb: Annotated[int, Field(ge=1, le=32_768)] = 1024,
        background: bool = False,
        root: str | None = None,
        replace_scope: bool = False,
        ctx: Context | None = None,
    ) -> str:
        return ingest_code(
            _resolve_service(registry, ctx), paths, calls, max_file_kb, background, root, replace_scope
        )

    @server.tool(name="ingest_docs", structured_output=False, description=_doc(ingest_docs))
    @_mcp_result
    def ingest_docs_tool(
        paths: Annotated[list[str], Field(max_length=64)],
        sections: bool = True,
        label: str = "Chunk",
        background: bool = False,
        json_mode: Literal["records", "document"] = "records",
        ctx: Context | None = None,
    ) -> str:
        return ingest_docs(
            _resolve_service(registry, ctx), paths, sections, label, background, json_mode
        )

    @server.tool(name="job_status", structured_output=False, description=_doc(job_status))
    @_mcp_result
    def job_status_tool(job_id: str, ctx: Context | None = None) -> str:
        return job_status(_resolve_service(registry, ctx), job_id)

    # Handles for lifecycle management (run closes the registry) and for
    # tests. grag_service is the default service for back-compat; in multi-db
    # mode with no determinable default there isn't one, so it is None.
    # Single-db clients must not complete initialization over a broken database.
    try:
        server.grag_service = registry.get()  # type: ignore[attr-defined]
    except GragError:
        if config.db_dir is None:
            registry.close()
            raise
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
