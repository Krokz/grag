"""REST API for grag — thin FastAPI layer over GragService.

Every endpoint maps 1:1 to a frozen contract model in grag.core.types and to
an MCP tool of the same payload. The app object is built here; uvicorn is
started by the CLI, not this module.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import grag
from grag.config import GragConfig, database_identity
from grag.core.errors import (
    ConfigurationError,
    GragError,
    NotFoundError,
    ReadOnlyViolation,
)
from grag.core.types import (
    CodeIngestRequest,
    CodeIngestResponse,
    ContextRequest,
    ContextResponse,
    DefineSchemaRequest,
    GraphSample,
    IngestRequest,
    IngestResponse,
    JobRecord,
    MutationSummary,
    QueryRequest,
    QueryResponse,
    SchemaDocument,
    SearchRequest,
    SearchResponse,
    UpsertEdgesRequest,
    UpsertNodesRequest,
)
from grag.registry import ServiceRegistry
from grag.service import GragService

_STATIC_DIR = Path(__file__).parent / "static"

DB_HEADER = "x-grag-db"

log = logging.getLogger(__name__)

# Binding to a wildcard address is an explicit "expose me" decision: Host
# validation can't help there (every Host is legitimate), so api_token should
# be set instead.
_WILDCARD_BINDS = {"", "0.0.0.0", "::"}  # noqa: S104 — a constant, not a bind

_SHUTDOWN_PATH = "/api/admin/stop"


def _is_loopback_bind(host: str) -> bool:
    """Return whether an explicit bind host is confined to this machine.

    Only the special hostname ``localhost`` and numeric loopback addresses are
    trusted. Other hostnames are deliberately not resolved here: DNS can
    change between validation and bind time. Brackets are accepted around an
    IPv6 literal to match common host notation.
    """

    candidate = host.strip().strip("[]")
    if candidate.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _validate_bind_security(config: GragConfig) -> None:
    """Refuse a network-visible REST/UI/MCP server without authentication."""

    if not _is_loopback_bind(config.host) and not config.api_token:
        raise ConfigurationError(
            "REST/UI server requires GRAG_API_TOKEN on a non-loopback host.",
            hint="Set GRAG_API_TOKEN or bind to 127.0.0.1/::1/localhost.",
        )


def _validate_mounted_mcp_path(path: str | None) -> None:
    """Keep the MCP mount from shadowing REST health/control routes or the SPA."""
    if path is None:
        return
    try:
        parsed = urlsplit(path)
    except ValueError:
        parsed = None
    invalid_character = any(
        char.isspace()
        or ord(char) < 0x20
        or ord(char) == 0x7F
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in path
    )
    if (
        parsed is None
        or not path.startswith("/")
        or path.startswith("//")
        or path == "/"
        or path == "/api"
        or path.startswith("/api/")
        or path == "/assets"
        or path.startswith("/assets/")
        or parsed.scheme
        or parsed.netloc
        or "?" in path
        or "#" in path
        or "\\" in path
        or invalid_character
    ):
        raise ConfigurationError(
            "Mounted MCP path must be a clean URL path under '/' and cannot overlap "
            "'/', '/api', or '/assets'.",
            hint="Use the default /mcp or another dedicated prefix such as /agent-mcp.",
        )


def _allowed_hosts(config: GragConfig) -> list[str]:
    """Host-header allow-list — the REST layer's DNS-rebinding guard.

    Loopbacks plus the configured bind host; "testserver" keeps FastAPI's
    TestClient working (it is not a publicly resolvable name, so a rebinding
    page cannot produce it)."""
    if config.host in _WILDCARD_BINDS:
        return ["*"]
    hosts = ["127.0.0.1", "localhost", "::1", "[::1]", "testserver"]
    if config.host not in hosts:
        hosts.append(config.host)
    return hosts


def _error_body(message: str, hint: str | None) -> dict:
    return {"error": message, "hint": hint}


def create_app(config: GragConfig) -> FastAPI:
    # Validate before opening a database or constructing the optional MCP app.
    # This single gate protects REST, the bundled UI, and a mounted MCP endpoint.
    _validate_bind_security(config)
    _validate_mounted_mcp_path(config.mcp_path)

    # One registry for the whole process: REST + UI + (optionally) MCP share
    # it, so a single GragService/write-conn serves every surface of a .lbdb.
    registry = ServiceRegistry(config)

    # Build the MCP streamable-http app first (when enabled) so its session
    # manager can be driven by this app's lifespan below. It shares the single
    # registry — passing it in is what makes single-process mode safe.
    mcp_session_manager = None
    mcp_app = None
    mcp_path = config.mcp_path
    if mcp_path:
        from grag.mcp_server.server import create_server

        mcp_server = create_server(config, registry=registry)
        mcp_app = mcp_server.streamable_http_app(
            streamable_http_path="/", stateless_http=True, host=config.host
        )
        mcp_session_manager = mcp_server.session_manager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if mcp_session_manager is not None:
                async with mcp_session_manager.run():
                    yield
            else:
                yield
        finally:
            # Close/checkpoint every Engine even when startup or request
            # teardown propagates an exception through the lifespan context.
            registry.close()

    app = FastAPI(title="grag", version=grag.__version__, lifespan=lifespan)
    app.state.registry = registry
    # Resolve the default lazily and tolerate failure: in multi-db mode with no
    # determinable default (2+ DBs, none named after db_path), registry.get()
    # raises at startup and the server would never come up — taking /api/dbs
    # (discovery) and all explicitly-selected requests down with it. Startup must
    # not depend on a default existing; only a request with no selector does.
    try:
        app.state.service = registry.get()
    except GragError:
        app.state.service = None

    # Mount MCP before the SPA catch-all at "/" so the MCP path is matched first.
    if mcp_app is not None and mcp_path is not None:
        app.mount(mcp_path, mcp_app, name="mcp")

    # Middleware stack (last added = outermost): TrustedHost rejects
    # DNS-rebinding Host headers before anything else runs; CORS then handles
    # preflights for explicitly configured origins; bearer auth runs last, so
    # it never sees CORS preflights (browsers don't send credentials on them).
    if config.api_token:
        expected = f"Bearer {config.api_token}"

        @app.middleware("http")
        async def require_bearer(request: Request, call_next):
            path = request.url.path
            protected = path.startswith("/api/") and path != "/api/health"
            if config.mcp_path and path.startswith(config.mcp_path):
                protected = True
            # The managed stop hook has a separate high-entropy token. Keep it
            # exempt even if a broad custom MCP path also matched this URL.
            if path == _SHUTDOWN_PATH:
                protected = False
            if protected and not hmac.compare_digest(
                request.headers.get("authorization", ""), expected
            ):
                return JSONResponse(
                    status_code=401,
                    content=_error_body(
                        "Unauthorized.",
                        "Send 'Authorization: Bearer <token>' (GRAG_API_TOKEN).",
                    ),
                )
            return await call_next(request)

    # Same-origin UI needs no CORS at all; only origins explicitly configured
    # via GRAG_CORS_ORIGINS get cross-origin access. No credentials: there are
    # no cookies/sessions, and credentialed wildcard CORS is spec-invalid.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "authorization", DB_HEADER],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts(config))

    def resolve(request: Request) -> GragService:
        # Single-db mode: db selectors are ignored, never an error.
        if config.db_dir is None:
            return app.state.registry.get()
        name = request.query_params.get("db") or request.headers.get(DB_HEADER)
        if name:
            return app.state.registry.get(name)
        # No selector: use the pre-resolved default if one exists, else surface
        # the registry's ConfigurationError (400) listing available DBs.
        if app.state.service is not None:
            return app.state.service
        return app.state.registry.get()

    # -- error mapping ---------------------------------------------------------

    @app.exception_handler(GragError)
    async def grag_error_handler(_: Request, exc: GragError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            status = 404
        elif isinstance(exc, ReadOnlyViolation):
            status = 403
        else:
            status = 400
        return JSONResponse(
            status_code=status, content=_error_body(exc.message, exc.hint)
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Detail stays in the server log; clients get a generic body (raw
        # str(exc) leaks paths and driver internals).
        log.exception("Unhandled %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500, content=_error_body("Internal server error.", None)
        )

    # -- endpoints (contract: see grag.core.types docstring) --------------------

    @app.get("/api/health")
    def health() -> dict:
        service = app.state.service
        identity = (
            database_identity(service.config.db_path) if service is not None else None
        )
        server_target = config.db_dir if config.db_dir is not None else config.db_path
        return {
            "status": "ok",
            "version": grag.__version__,
            "database_id": identity,
            "server_id": database_identity(server_target),
            "pid": os.getpid(),
            "mcp_enabled": config.mcp_path is not None,
            "mcp_path": config.mcp_path,
            # None when no embedder is configured; otherwise the background
            # worker's counters (running / idle / embedded_total / last_error).
            "embedding": service.embedding_status() if service is not None else None,
        }

    @app.post(_SHUTDOWN_PATH, include_in_schema=False, status_code=202)
    def request_managed_shutdown(request: Request):
        """Ask a managed daemon to stop without exposing a general API route."""

        token = getattr(app.state, "shutdown_token", None)
        callback = getattr(app.state, "request_shutdown", None)
        if not token or not callable(callback):
            return JSONResponse(
                status_code=404,
                content=_error_body("Not found.", None),
            )
        supplied = request.headers.get("x-grag-stop-token", "")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse(
                status_code=401,
                content=_error_body("Unauthorized.", None),
            )
        callback()
        return {"status": "stopping"}

    @app.get("/api/dbs")
    def list_dbs() -> dict:
        default = None
        if config.db_dir is not None and app.state.service is not None:
            # The default service was resolved by the registry at startup, so
            # its db_path is the resolved default .lbdb file. None when no
            # default could be determined (2+ DBs, none preferred).
            default = app.state.service.config.db_path.stem
        return {"dbs": app.state.registry.list_dbs(), "default": default}

    @app.get("/api/schema")
    def describe_schema(request: Request, format: str | None = Query(default=None)):
        doc = resolve(request).describe_schema()
        if format == "text":
            return PlainTextResponse(doc.text)
        return doc

    @app.post("/api/schema/define", response_model=SchemaDocument)
    def define_schema(request: Request, req: DefineSchemaRequest) -> SchemaDocument:
        return resolve(request).define_schema(req)

    @app.post("/api/nodes/upsert", response_model=MutationSummary)
    def upsert_nodes(request: Request, req: UpsertNodesRequest) -> MutationSummary:
        return resolve(request).upsert_nodes(req)

    @app.post("/api/edges/upsert", response_model=MutationSummary)
    def upsert_edges(request: Request, req: UpsertEdgesRequest) -> MutationSummary:
        return resolve(request).upsert_edges(req)

    @app.post("/api/query", response_model=QueryResponse)
    def cypher_query(request: Request, req: QueryRequest) -> QueryResponse:
        return resolve(request).cypher_query(req)

    @app.post("/api/search", response_model=SearchResponse)
    def search_knowledge(request: Request, req: SearchRequest) -> SearchResponse:
        return resolve(request).search_knowledge(req)

    @app.post("/api/context", response_model=ContextResponse)
    def get_context(request: Request, req: ContextRequest) -> ContextResponse:
        return resolve(request).get_context(req)

    @app.post("/api/ingest", response_model=IngestResponse)
    def ingest(request: Request, req: IngestRequest) -> IngestResponse:
        return resolve(request).ingest(req)

    @app.post("/api/ingest/code", response_model=CodeIngestResponse)
    def ingest_code(request: Request, req: CodeIngestRequest) -> CodeIngestResponse:
        return resolve(request).ingest_code(req)

    # -- background jobs: long ingests return a job id instead of blocking -------

    @app.post("/api/jobs/ingest/code", response_model=JobRecord, status_code=202)
    def submit_ingest_code(request: Request, req: CodeIngestRequest) -> JobRecord:
        return resolve(request).submit_ingest_code(req)

    @app.post("/api/jobs/ingest", response_model=JobRecord, status_code=202)
    def submit_ingest(request: Request, req: IngestRequest) -> JobRecord:
        return resolve(request).submit_ingest(req)

    @app.get("/api/jobs")
    def list_jobs(request: Request, limit: int = Query(default=50)) -> dict:
        return {"jobs": [j.model_dump() for j in resolve(request).list_jobs(limit)]}

    @app.get("/api/jobs/{job_id}", response_model=JobRecord)
    def get_job(request: Request, job_id: str) -> JobRecord:
        return resolve(request).get_job(job_id)

    @app.get("/api/export")
    def export_jsonl(request: Request) -> StreamingResponse:
        """Online backup: stream the portable JSONL export of the live database.

        The CLI's `grag export` needs exclusive access to the .lbdb (single
        writer); a serving process cannot be stopped for every backup, so
        the same stream is offered here. Bearer-protected like every /api
        route. Reads run on pooled connections, so writes are not blocked.
        """
        from grag.transfer import export_lines

        engine = resolve(request).engine

        def body():
            for line in export_lines(engine):
                yield line + "\n"

        name = resolve(request).config.db_path.stem or "grag"
        return StreamingResponse(
            body(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{name}.jsonl"'},
        )

    @app.get("/api/graph/sample", response_model=GraphSample)
    def graph_sample(
        request: Request,
        limit: int = Query(default=200),
        label: str | None = Query(default=None),
    ) -> GraphSample:
        return resolve(request).graph_sample(limit=limit, label=label)

    @app.get("/api/graph/full", response_model=GraphSample)
    def graph_full(request: Request) -> GraphSample:
        return resolve(request).graph_full()

    # -- UI statics --------------------------------------------------------------

    if (_STATIC_DIR / "index.html").is_file():
        from fastapi.responses import FileResponse
        from starlette.exceptions import HTTPException as StarletteHTTPException

        @app.exception_handler(StarletteHTTPException)
        async def spa_fallback(_: Request, exc: StarletteHTTPException):
            # Unknown non-API GET paths serve the SPA (deep links); real API
            # misses and non-GETs keep their 404.
            request = _
            if (
                exc.status_code == 404
                and request.method == "GET"
                and not request.url.path.startswith("/api/")
            ):
                return FileResponse(_STATIC_DIR / "index.html")
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail}
            )

        app.mount(
            "/",
            StaticFiles(directory=_STATIC_DIR, html=True),
            name="ui",
        )
    else:

        @app.get("/")
        def root() -> dict:
            return {
                "service": "grag",
                "version": grag.__version__,
                "ui": "not built — see ui/",
            }

    return app
