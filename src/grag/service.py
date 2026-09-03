"""Facade over the core/retrieval/ingest modules — the single entry point used
by the REST layer, the MCP server, and the CLI.

Method signatures are frozen. Implementations delegate to module functions
lazily, so this facade works from Wave 0 onward: a method whose module has not
landed yet raises GragError("not implemented") instead of ImportError.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from grag.config import GragConfig
from grag.core.engine import (
    Engine,
    EngineResult,
    drop_internal_rows,
    extract_subgraph,
    is_internal_label,
)
from grag.core.errors import GragError, NotFoundError, ReadOnlyViolation, SchemaError
from grag.core.ident import validate_identifier
from grag.core.types import (
    CodeIngestRequest,
    CodeIngestResponse,
    ContextRequest,
    ContextResponse,
    DefineSchemaRequest,
    GraphSample,
    GraphStats,
    IngestRequest,
    IngestResponse,
    JobRecord,
    MutationSummary,
    QueryRequest,
    QueryResponse,
    SchemaDocument,
    SearchRequest,
    SearchResponse,
    Subgraph,
    UpsertEdgesRequest,
    UpsertNodesRequest,
    merge_subgraphs,
)

if TYPE_CHECKING:
    from grag.embedworker import EmbedWorker
    from grag.jobs import JobManager
    from grag.refresh import CodeIndexRefresher

# Write keywords rejected on the read-only cypher_query path. Guardrail, not a
# security boundary — the LLM contract steers writes to the upsert tools.
# Matching runs on the statement with string literals and comments blanked
# out, so a read like WHERE n.title = 'Set Theory' doesn't trip it.
_LITERALS = re.compile(
    r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|//[^\n]*|/\*.*?\*/", re.DOTALL
)
_WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|DROP|ALTER|COPY|INSTALL|LOAD|UNINSTALL)\b",
    re.IGNORECASE,
)
# CALL procedures with side effects (CREATE_VECTOR_INDEX, DROP_FTS_INDEX, ...)
# slip past the keyword pattern — underscores are word chars, so \bCREATE\b
# doesn't match inside them. CALL is allowed only for known read procedures.
_READ_CALL = re.compile(
    r"\bCALL\s+(?:QUERY_[A-Z_]*|TABLE_INFO|SHOW_TABLES|CURRENT_SETTING|DB_VERSION)\b",
    re.IGNORECASE,
)
_ANY_CALL = re.compile(r"\bCALL\b", re.IGNORECASE)
_LIMIT_PATTERN = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_UNION_PATTERN = re.compile(r"\bUNION\b", re.IGNORECASE)


def _assert_read_only(cypher: str) -> None:
    scrubbed = _LITERALS.sub(" ", cypher)
    violates = _WRITE_PATTERN.search(scrubbed) is not None
    if not violates:
        violates = any(
            not _READ_CALL.match(scrubbed, m.start())
            for m in _ANY_CALL.finditer(scrubbed)
        )
    if violates:
        raise ReadOnlyViolation(
            "cypher_query is read-only; the statement contains a write keyword.",
            hint="Use define_schema / upsert_nodes / upsert_edges for writes.",
        )


def _with_limit(cypher: str, limit: int) -> str:
    """Append a LIMIT unless the statement already bounds itself. The caller
    passes limit+1 so one extra row proves truncation without materializing
    the full result set. UNION is left alone: an appended LIMIT would bind to
    the last branch only."""
    scrubbed = _LITERALS.sub(" ", cypher)
    if _LIMIT_PATTERN.search(scrubbed) or _UNION_PATTERN.search(scrubbed):
        return cypher
    return cypher.rstrip().rstrip(";") + f"\nLIMIT {limit}"


def _not_implemented(module: str) -> GragError:
    return GragError(
        f"Module '{module}' is not implemented yet.",
        hint="This facade method is frozen; the module lands in a later build wave.",
    )


class GragService:
    def __init__(self, config: GragConfig | None = None):
        self.config = config or GragConfig.from_env()
        self.engine = Engine(self.config)
        self.embed_worker: EmbedWorker | None = None
        # Background ingest jobs (POST /api/jobs/...), created lazily.
        self._jobs: JobManager | None = None
        # Drift detector for indexed checkouts (serving processes only).
        self.refresher: CodeIndexRefresher | None = None

    def close(self) -> None:
        if self.embed_worker is not None:
            self.embed_worker.stop()
            self.embed_worker = None
        if self._jobs is not None:
            self._jobs.shutdown()
        self.engine.close()

    # -- background embedding -------------------------------------------------------

    def start_background_embedding(self) -> bool:
        """Attach an EmbedWorker so ingests/searches never embed inline.

        No-op (False) without an embedder. Serving processes call this via
        ServiceRegistry; one-shot CLI commands keep the synchronous path.
        """
        if self.config.embedder is None:
            return False
        if self.embed_worker is None:
            from grag.embedworker import EmbedWorker

            worker = EmbedWorker(self.engine, self.config)
            self.engine.embed_worker = worker  # type: ignore[attr-defined]
            worker.start()
            self.embed_worker = worker
        return True

    def embedding_status(self) -> dict | None:
        return None if self.embed_worker is None else self.embed_worker.status()

    # -- code-index auto refresh ---------------------------------------------------

    def enable_auto_refresh(self) -> None:
        """Re-ingest indexed checkouts when their git state moves (see grag.refresh)."""
        if self.refresher is None:
            from grag.refresh import CodeIndexRefresher

            self.refresher = CodeIndexRefresher(
                self, interval=self.config.auto_refresh_interval_s
            )

    def _refresh_status(self) -> str | None:
        return None if self.refresher is None else self.refresher.maybe_refresh()

    def refresh_status(self) -> dict | None:
        return None if self.refresher is None else self.refresher.status()

    # -- background jobs -----------------------------------------------------------

    @property
    def jobs(self) -> JobManager:
        if self._jobs is None:
            from grag.jobs import JobManager

            self._jobs = JobManager()
        return self._jobs

    # -- schema ---------------------------------------------------------------

    def describe_schema(self) -> SchemaDocument:
        try:
            from grag.core.schema import build_schema_document
        except ImportError:
            raise _not_implemented("grag.core.schema") from None
        return build_schema_document(self.engine, self.config)

    def define_schema(self, req: DefineSchemaRequest) -> SchemaDocument:
        try:
            from grag.core.mutate import define_schema
        except ImportError:
            raise _not_implemented("grag.core.mutate") from None
        return define_schema(self.engine, self.config, req)

    # -- mutation --------------------------------------------------------------

    def upsert_nodes(self, req: UpsertNodesRequest) -> MutationSummary:
        try:
            from grag.core.mutate import upsert_nodes
        except ImportError:
            raise _not_implemented("grag.core.mutate") from None
        summary = upsert_nodes(self.engine, self.config, req)
        if self.embed_worker is not None:
            self.embed_worker.wake()
        return summary

    def upsert_edges(self, req: UpsertEdgesRequest) -> MutationSummary:
        try:
            from grag.core.mutate import upsert_edges
        except ImportError:
            raise _not_implemented("grag.core.mutate") from None
        return upsert_edges(self.engine, self.config, req)

    # -- query ------------------------------------------------------------------

    def cypher_query(self, req: QueryRequest) -> QueryResponse:
        _assert_read_only(req.cypher)
        self._refresh_status()
        limit = req.limit or self.config.default_query_limit
        limit = max(1, min(limit, self.config.max_query_limit))
        # Hide internal tables (_grag_tables & friends): a generic MATCH (n)
        # spans them, but they are not part of the user's data model. The
        # filter runs after the pushed-down LIMIT, so on a bare MATCH (n) a
        # few internal rows can consume result budget — truncated stays the
        # signal that more rows exist.
        result = drop_internal_rows(
            self.engine.execute(_with_limit(req.cypher, limit + 1))
        )
        truncated = len(result.rows) > limit
        rows = result.rows[:limit]
        sub = extract_subgraph(EngineResult(result.columns, rows), self._pk_map())
        return QueryResponse(
            columns=result.columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            subgraph=sub,
        )

    def _pk_map(self) -> dict[str, str] | None:
        try:
            from grag.core.schema import pk_map
        except ImportError:
            return None
        return pk_map(self.engine)

    # -- retrieval -----------------------------------------------------------------

    def search_knowledge(self, req: SearchRequest) -> SearchResponse:
        try:
            from grag.retrieval.search import search_knowledge
        except ImportError:
            raise _not_implemented("grag.retrieval.search") from None
        req.hops = max(0, min(req.hops, self.config.max_hops))
        index_status = self._refresh_status()
        resp = search_knowledge(self.engine, self.config, req)
        resp.index_status = index_status  # type: ignore[assignment]
        return resp

    def get_context(self, req: ContextRequest) -> ContextResponse:
        try:
            from grag.retrieval.context import get_context
        except ImportError:
            raise _not_implemented("grag.retrieval.context") from None
        req.hops = max(0, min(req.hops, self.config.max_hops))
        self._refresh_status()
        return get_context(self.engine, self.config, req)

    # -- ingestion --------------------------------------------------------------------

    def ingest(self, req: IngestRequest) -> IngestResponse:
        try:
            from grag.ingest.loaders import ingest_documents
        except ImportError:
            raise _not_implemented("grag.ingest.loaders") from None
        return ingest_documents(self.engine, self.config, req)

    def ingest_code(self, req: CodeIngestRequest) -> CodeIngestResponse:
        try:
            from grag.ingest.code import ingest_code
        except ImportError:
            raise _not_implemented("grag.ingest.code") from None
        return ingest_code(self.engine, self.config, req)

    # -- background ingest jobs ---------------------------------------------------------

    def submit_ingest_code(self, req: CodeIngestRequest) -> JobRecord:
        """Queue ingest_code on the service's job thread; poll with get_job."""
        return self.jobs.submit(
            "ingest_code", lambda: self.ingest_code(req), req.model_dump()
        )

    def submit_ingest(self, req: IngestRequest) -> JobRecord:
        params = req.model_dump(exclude={"documents"})
        params["documents"] = len(req.documents)
        return self.jobs.submit("ingest", lambda: self.ingest(req), params)

    def get_job(self, job_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError(
                f"No job with id {job_id!r}.",
                hint="Jobs live in memory for the serving process; list them via "
                "GET /api/jobs.",
            )
        return job

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return self.jobs.list(limit)

    # -- ui -----------------------------------------------------------------------------

    def graph_sample(self, limit: int = 200, label: str | None = None) -> GraphSample:
        limit = max(1, min(limit, self.config.max_query_limit))
        safe_label = validate_identifier(label, "node label") if label else None
        if safe_label:
            known_labels = {table.name for table in self.describe_schema().node_tables}
            if safe_label not in known_labels:
                raise SchemaError(
                    f"Unknown node label {safe_label!r}.",
                    hint=f"Available node labels: {sorted(known_labels)}.",
                )
        pattern = f"(n:{safe_label})" if safe_label else "(n)"
        pk_map = self._pk_map()
        sub = extract_subgraph(
            self.engine.execute(f"MATCH {pattern} RETURN n LIMIT {limit}"),
            pk_map,
        )
        try:
            if safe_label:
                # Label-scoped: only the 1-hop neighborhood of matching nodes,
                # so a label view shows that label *and its relationships*
                # instead of the whole graph's edges (the leaky-merge papercut).
                rels = self.engine.execute(
                    f"MATCH (a:{safe_label})-[r]-(b) RETURN a, r, b LIMIT {limit}"
                )
            else:
                rels = self.engine.execute(
                    f"MATCH (a)-[r]->(b) RETURN a, r, b LIMIT {limit}"
                )
            sub = merge_subgraphs(sub, extract_subgraph(rels, pk_map))
        except GragError:
            pass  # no rel tables yet
        nodes = [n for n in sub.nodes if not is_internal_label(n.label)]
        kept = {n.id for n in nodes}
        sub = Subgraph(
            nodes=nodes,
            edges=[
                e
                for e in sub.edges
                if not is_internal_label(e.type)
                and e.source in kept
                and e.target in kept
            ],
        )
        return GraphSample(subgraph=sub, stats=self._stats())

    def graph_full(self) -> GraphSample:
        """Every user node and edge, unclamped — for whole-database exports.

        Unlike ``graph_sample`` this ignores ``max_query_limit`` on purpose:
        the UI's full-graph SVG export needs the mass of the graph, not a
        window into it. Internal ``_``-prefixed tables are still dropped.
        """
        pk_map = self._pk_map()
        sub = extract_subgraph(self.engine.execute("MATCH (n) RETURN n"), pk_map)
        try:
            rels = self.engine.execute("MATCH (a)-[r]->(b) RETURN a, r, b")
            sub = merge_subgraphs(sub, extract_subgraph(rels, pk_map))
        except GragError:
            pass  # no rel tables yet
        nodes = [n for n in sub.nodes if not is_internal_label(n.label)]
        kept = {n.id for n in nodes}
        sub = Subgraph(
            nodes=nodes,
            edges=[
                e
                for e in sub.edges
                if not is_internal_label(e.type)
                and e.source in kept
                and e.target in kept
            ],
        )
        return GraphSample(subgraph=sub, stats=self._stats())

    def _stats(self) -> GraphStats:
        try:
            from grag.core.schema import table_stats
        except ImportError:
            return GraphStats()
        return table_stats(self.engine)
