"""REST API for grag — thin FastAPI layer over GragService.

Every endpoint maps 1:1 to a frozen contract model in grag.core.types and to
an MCP tool of the same payload. The app object is built here; uvicorn is
started by the CLI, not this module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import fastapi
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import grag
from grag.config import GragConfig
from grag.core.errors import GragError, NotFoundError, ReadOnlyViolation
from grag.core.types import (
    ContextRequest,
    ContextResponse,
    DefineSchemaRequest,
    GraphSample,
    IngestRequest,
    IngestResponse,
    MutationSummary,
    QueryRequest,
    QueryResponse,
    SchemaDocument,
    SearchRequest,
    SearchResponse,
    UpsertEdgesRequest,
    UpsertNodesRequest,
)
from grag.service import GragService

_STATIC_DIR = Path(__file__).parent / "static"


def _error_body(message: str, hint: str | None) -> dict:
    return {"error": message, "hint": hint}


def create_app(config: GragConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.service.close()

    app = FastAPI(title="grag", version=grag.__version__, lifespan=lifespan)
    app.state.service = GragService(config)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def service() -> GragService:
        return app.state.service

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
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content=_error_body(str(exc), None)
        )

    # -- endpoints (contract: see grag.core.types docstring) --------------------

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": grag.__version__}

    @app.get("/api/schema")
    def describe_schema(format: str | None = Query(default=None)):
        doc = service().describe_schema()
        if format == "text":
            return PlainTextResponse(doc.text)
        return doc

    @app.post("/api/schema/define", response_model=SchemaDocument)
    def define_schema(req: DefineSchemaRequest) -> SchemaDocument:
        return service().define_schema(req)

    @app.post("/api/nodes/upsert", response_model=MutationSummary)
    def upsert_nodes(req: UpsertNodesRequest) -> MutationSummary:
        return service().upsert_nodes(req)

    @app.post("/api/edges/upsert", response_model=MutationSummary)
    def upsert_edges(req: UpsertEdgesRequest) -> MutationSummary:
        return service().upsert_edges(req)

    @app.post("/api/query", response_model=QueryResponse)
    def cypher_query(req: QueryRequest) -> QueryResponse:
        return service().cypher_query(req)

    @app.post("/api/search", response_model=SearchResponse)
    def search_knowledge(req: SearchRequest) -> SearchResponse:
        return service().search_knowledge(req)

    @app.post("/api/context", response_model=ContextResponse)
    def get_context(req: ContextRequest) -> ContextResponse:
        return service().get_context(req)

    @app.post("/api/ingest", response_model=IngestResponse)
    def ingest(req: IngestRequest) -> IngestResponse:
        return service().ingest(req)

    @app.get("/api/graph/sample", response_model=GraphSample)
    def graph_sample(
        limit: int = Query(default=200), label: str | None = Query(default=None)
    ) -> GraphSample:
        return service().graph_sample(limit=limit, label=label)

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
