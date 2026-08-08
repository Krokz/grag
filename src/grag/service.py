"""Facade over the core/retrieval/ingest modules — the single entry point used
by the REST layer, the MCP server, and the CLI.

Method signatures are frozen. Implementations delegate to module functions
lazily, so this facade works from Wave 0 onward: a method whose module has not
landed yet raises GragError("not implemented") instead of ImportError.
"""

from __future__ import annotations

import re

from grag.config import GragConfig
from grag.core.engine import (
    Engine,
    EngineResult,
    drop_internal_rows,
    extract_subgraph,
    is_internal_label,
)
from grag.core.errors import GragError, ReadOnlyViolation
from grag.core.types import (
    ContextRequest,
    ContextResponse,
    DefineSchemaRequest,
    GraphSample,
    GraphStats,
    IngestRequest,
    IngestResponse,
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

# Write keywords rejected on the read-only cypher_query path. Guardrail, not a
# security boundary — the LLM contract steers writes to the upsert tools.
_WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|DROP|ALTER|COPY|INSTALL|LOAD)\b", re.IGNORECASE
)


def _not_implemented(module: str) -> GragError:
    return GragError(
        f"Module '{module}' is not implemented yet.",
        hint="This facade method is frozen; the module lands in a later build wave.",
    )


class GragService:
    def __init__(self, config: GragConfig | None = None):
        self.config = config or GragConfig.from_env()
        self.engine = Engine(self.config)

    def close(self) -> None:
        self.engine.close()

    # -- schema ---------------------------------------------------------------

    def describe_schema(self) -> SchemaDocument:
        try:
            from grag.core.schema import build_schema_document
        except ImportError:
            raise _not_implemented("grag.core.schema")
        return build_schema_document(self.engine, self.config)

    def define_schema(self, req: DefineSchemaRequest) -> SchemaDocument:
        try:
            from grag.core.mutate import define_schema
        except ImportError:
            raise _not_implemented("grag.core.mutate")
        return define_schema(self.engine, self.config, req)

    # -- mutation --------------------------------------------------------------

    def upsert_nodes(self, req: UpsertNodesRequest) -> MutationSummary:
        try:
            from grag.core.mutate import upsert_nodes
        except ImportError:
            raise _not_implemented("grag.core.mutate")
        return upsert_nodes(self.engine, self.config, req)

    def upsert_edges(self, req: UpsertEdgesRequest) -> MutationSummary:
        try:
            from grag.core.mutate import upsert_edges
        except ImportError:
            raise _not_implemented("grag.core.mutate")
        return upsert_edges(self.engine, self.config, req)

    # -- query ------------------------------------------------------------------

    def cypher_query(self, req: QueryRequest) -> QueryResponse:
        if _WRITE_PATTERN.search(req.cypher):
            raise ReadOnlyViolation(
                "cypher_query is read-only; the statement contains a write keyword.",
                hint="Use define_schema / upsert_nodes / upsert_edges for writes.",
            )
        limit = req.limit or self.config.default_query_limit
        limit = max(1, min(limit, self.config.max_query_limit))
        # Hide internal tables (_grag_tables & friends): a generic MATCH (n)
        # spans them, but they are not part of the user's data model.
        result = drop_internal_rows(self.engine.execute(req.cypher))
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
            raise _not_implemented("grag.retrieval.search")
        req.hops = max(0, min(req.hops, self.config.max_hops))
        return search_knowledge(self.engine, self.config, req)

    def get_context(self, req: ContextRequest) -> ContextResponse:
        try:
            from grag.retrieval.context import get_context
        except ImportError:
            raise _not_implemented("grag.retrieval.context")
        req.hops = max(0, min(req.hops, self.config.max_hops))
        return get_context(self.engine, self.config, req)

    # -- ingestion --------------------------------------------------------------------

    def ingest(self, req: IngestRequest) -> IngestResponse:
        try:
            from grag.ingest.loaders import ingest_documents
        except ImportError:
            raise _not_implemented("grag.ingest.loaders")
        return ingest_documents(self.engine, self.config, req)

    # -- ui -----------------------------------------------------------------------------

    def graph_sample(self, limit: int = 200, label: str | None = None) -> GraphSample:
        limit = max(1, min(limit, self.config.max_query_limit))
        pattern = f"(n:{label})" if label else "(n)"
        sub = extract_subgraph(
            self.engine.execute(f"MATCH {pattern} RETURN n LIMIT {limit}"), self._pk_map()
        )
        try:
            rels = self.engine.execute(
                f"MATCH (a)-[r]->(b) RETURN a, r, b LIMIT {limit}"
            )
            sub = merge_subgraphs(sub, extract_subgraph(rels, self._pk_map()))
        except GragError:
            pass  # no rel tables yet
        nodes = [n for n in sub.nodes if not is_internal_label(n.label)]
        kept = {n.id for n in nodes}
        sub = Subgraph(
            nodes=nodes,
            edges=[
                e
                for e in sub.edges
                if not is_internal_label(e.type) and e.source in kept and e.target in kept
            ],
        )
        return GraphSample(subgraph=sub, stats=self._stats())

    def _stats(self) -> GraphStats:
        try:
            from grag.core.schema import table_stats
        except ImportError:
            return GraphStats()
        return table_stats(self.engine)
