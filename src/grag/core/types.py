"""Frozen data contracts for grag.

Every layer (core, retrieval, api, mcp_server, ingest, ui) codes against the
models and constants in this module. Treat as immutable.

Tool / endpoint contract (MCP tool = REST endpoint, same payloads):

    describe_schema()                 GET  /api/schema         -> SchemaDocument
    define_schema(req)                POST /api/schema/define  -> SchemaDocument
    upsert_nodes(req)                 POST /api/nodes/upsert   -> MutationSummary
    upsert_edges(req)                 POST /api/edges/upsert   -> MutationSummary
    cypher_query(req)                 POST /api/query          -> QueryResponse
    search_knowledge(req)             POST /api/search         -> SearchResponse
    get_context(req)                  POST /api/context        -> ContextResponse
    ingest_code(req)                  POST /api/ingest/code    -> CodeIngestResponse
    (ingest)                          POST /api/ingest         -> IngestResponse
    (ui)                              GET  /api/graph/sample   -> GraphSample
    (ui)                              GET  /api/graph/full     -> GraphSample
    (ui)                              GET  /api/health         -> {"status": "ok", "version": str}

Internal module contract (implemented by later waves, called via grag.service):

    grag.core.schema.build_schema_document(engine, config) -> SchemaDocument
    grag.core.schema.pk_map(engine) -> dict[str, str]          # label -> pk prop
    grag.core.schema.table_stats(engine) -> GraphStats
    grag.core.mutate.define_schema(engine, config, DefineSchemaRequest) -> SchemaDocument
    grag.core.mutate.upsert_nodes(engine, config, UpsertNodesRequest) -> MutationSummary
    grag.core.mutate.upsert_edges(engine, config, UpsertEdgesRequest) -> MutationSummary
    grag.core.serialize.pack_context(subgraph, token_budget, seed_ids=None) -> PackedContext
    grag.retrieval.search.search_knowledge(engine, config, SearchRequest) -> SearchResponse
    grag.retrieval.context.get_context(engine, config, ContextRequest) -> ContextResponse
    grag.retrieval.vectors.vector_candidates(engine, config, query, labels, top_k) -> list[ScoredNode]
    grag.ingest.loaders.ingest_documents(engine, config, IngestRequest) -> IngestResponse
    grag.ingest.code.ingest_code(engine, config, CodeIngestRequest) -> CodeIngestResponse
    grag.api.main.create_app(config) -> fastapi.FastAPI
    grag.mcp_server.server.run(config) -> None

Storage conventions:

    * Properties starting with "_" are grag-internal (provenance, vector
      codes). Tool callers never write them directly; the mutation layer
      rejects them.
    * define_schema records every table in META_TABLE so introspection and
      canonical node ids work without parsing DDL. Tables created via raw
      cypher are still introspected (SHOW_TABLES / TABLE_INFO fallback).
    * Vector columns (EMBEDDING_PROP etc.) are added lazily via ALTER TABLE by
      the retrieval layer the first time embeddings are enabled for a table —
      never by the mutation layer.
    * FTS: one index per searchable node table, named fts_index_name(table),
      over its STRING props (excluding reserved/vector columns), created and
      maintained by the retrieval layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- reserved property conventions ---------------------------------------------

RESERVED_PREFIX = "_"
PROVENANCE_SOURCE = "_source"  # STRING: origin of a fact (file, url, doc id)
PROVENANCE_CREATED_AT = "_created_at"  # TIMESTAMP: write time

EMBEDDING_PROP = "embedding"  # FLOAT[dim]: exact vector, rescore source of truth
EMB_MAGNITUDE_PROP = "_emb_r"  # DOUBLE: ||v|| at write time (polar split)
EMB_CODE_PROP = "_emb_code"  # UINT8[]: quantized direction codes
EMB_MODEL_PROP = "_emb_model"  # STRING: embedder model id

VECTOR_PROPS = {EMBEDDING_PROP, EMB_MAGNITUDE_PROP, EMB_CODE_PROP, EMB_MODEL_PROP}

# Registry of grag-managed tables:
# (name STRING, kind STRING['node'|'rel'], pk STRING, searchable BOOL,
#  from_label STRING, to_label STRING)
META_TABLE = "_grag_tables"

VectorCodec = Literal["fp32", "int8", "binary", "polar"]
CypherType = Literal["STRING", "INT64", "DOUBLE", "BOOL", "DATE", "TIMESTAMP"]


def fts_index_name(table: str) -> str:
    return f"grag_fts__{table}"


def make_node_id(label: str, key: Any) -> str:
    """Canonical external node id used across tools, API and UI."""
    return f"{label}:{key}"


def split_node_id(node_id: str) -> tuple[str, str]:
    """Inverse of make_node_id: 'Label:key' -> ('Label', 'key')."""
    label, _, key = node_id.partition(":")
    return label, key


# --- graph primitives -----------------------------------------------------------


class NodeRecord(BaseModel):
    id: str  # make_node_id(label, primary_key_value)
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)


class EdgeRecord(BaseModel):
    id: str  # f"{type}:{source}->{target}"
    type: str
    source: str  # NodeRecord.id of the source node
    target: str  # NodeRecord.id of the target node
    properties: dict[str, Any] = Field(default_factory=dict)


class Subgraph(BaseModel):
    nodes: list[NodeRecord] = Field(default_factory=list)
    edges: list[EdgeRecord] = Field(default_factory=list)

    def node_map(self) -> dict[str, NodeRecord]:
        return {n.id: n for n in self.nodes}


def merge_subgraphs(*subs: Subgraph) -> Subgraph:
    nodes: dict[str, NodeRecord] = {}
    edges: dict[str, EdgeRecord] = {}
    for sub in subs:
        for n in sub.nodes:
            nodes.setdefault(n.id, n)
        for e in sub.edges:
            edges.setdefault(e.id, e)
    return Subgraph(nodes=list(nodes.values()), edges=list(edges.values()))


# --- schema ---------------------------------------------------------------------


class PropertySpec(BaseModel):
    name: str
    type: CypherType = "STRING"


class NodeTableSpec(BaseModel):
    name: str
    primary_key: str = "id"
    properties: list[PropertySpec] = Field(default_factory=list)
    searchable: bool = True  # retrieval layer maintains an FTS index on STRING props


class RelTableSpec(BaseModel):
    name: str
    from_label: str
    to_label: str
    properties: list[PropertySpec] = Field(default_factory=list)


class DefineSchemaRequest(BaseModel):
    node_tables: list[NodeTableSpec] = Field(default_factory=list)
    rel_tables: list[RelTableSpec] = Field(default_factory=list)
    if_not_exists: bool = True


class PropertyDoc(BaseModel):
    name: str
    type: str
    is_primary_key: bool = False


class NodeTableDoc(BaseModel):
    name: str
    properties: list[PropertyDoc] = Field(default_factory=list)
    row_count: int = 0
    sample_keys: list[str] = Field(default_factory=list)
    searchable: bool = False


class RelTableDoc(BaseModel):
    name: str
    from_label: str = ""
    to_label: str = ""
    properties: list[PropertyDoc] = Field(default_factory=list)
    row_count: int = 0


class SchemaDocument(BaseModel):
    """Full schema introspection. `text` is the prompt-shaped rendering an LLM
    anchors on before writing Cypher — keep it compact."""

    node_tables: list[NodeTableDoc] = Field(default_factory=list)
    rel_tables: list[RelTableDoc] = Field(default_factory=list)
    text: str = ""


# --- mutation -------------------------------------------------------------------


class UpsertNode(BaseModel):
    label: str
    key: Any  # primary key value
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None  # provenance -> _source


class UpsertEdge(BaseModel):
    type: str
    from_label: str
    from_key: Any
    to_label: str
    to_key: Any
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None


class UpsertNodesRequest(BaseModel):
    nodes: list[UpsertNode]


class UpsertEdgesRequest(BaseModel):
    edges: list[UpsertEdge]


class MutationSummary(BaseModel):
    nodes: int = 0
    edges: int = 0
    warnings: list[str] = Field(default_factory=list)


# --- query ----------------------------------------------------------------------


class QueryRequest(BaseModel):
    cypher: str
    limit: int | None = None  # clamped to [1, config.max_query_limit]


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    subgraph: Subgraph = Field(default_factory=Subgraph)


# --- retrieval ------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str
    top_k: int = 8
    hops: int = 1  # clamped to config.max_hops
    labels: list[str] | None = None  # restrict seed node tables
    token_budget: int | None = None  # for the included serialized context


class ScoredNode(BaseModel):
    node: NodeRecord
    score: float
    match: Literal["fts", "vector", "graph"]


class SearchResponse(BaseModel):
    seeds: list[ScoredNode] = Field(default_factory=list)
    subgraph: Subgraph = Field(default_factory=Subgraph)  # seeds + expansion
    context: str = ""  # token-budgeted serialization, ready for prompt injection
    # Nodes still awaiting an embedding after this search (the query path
    # embeds at most config.max_embed_per_search synchronously). > 0 means
    # vector recall improves as later searches drain the backlog.
    pending_embeddings: int = 0
    # None when vector search ran normally (or the query was empty). "off"
    # when no embedder is configured for this server process — search is
    # FTS-only and pending_embeddings is always 0, which otherwise looks
    # identical to "fully embedded." "error" when an embedder is configured
    # but the vector path raised (bad install, bad config) and silently
    # degraded to FTS-only for this call.
    vector_status: Literal["off", "error"] | None = None


class ContextRequest(BaseModel):
    node_ids: list[str]
    hops: int = 1
    token_budget: int | None = None


class ContextResponse(BaseModel):
    context: str
    token_estimate: int
    included_node_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    subgraph: Subgraph = Field(default_factory=Subgraph)


class PackedContext(BaseModel):
    """Internal result of serialize.pack_context."""

    text: str
    token_estimate: int
    included_node_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


# --- ingestion ------------------------------------------------------------------


class IngestDocument(BaseModel):
    text: str
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    documents: list[IngestDocument]
    label: str = "Chunk"
    chunk: bool = True
    chunk_size: int = 1200  # characters
    chunk_overlap: int = 150


class IngestResponse(BaseModel):
    label: str
    nodes_created: int
    nodes_pruned: int = 0


class CodeIngestRequest(BaseModel):
    paths: list[str]
    calls: bool = True
    max_file_kb: int = 1024
    # Skip the database writes for files whose content (and parse options)
    # match the hash recorded at their last ingest. Every file is still
    # parsed so cross-file IMPORTS/CALLS/INHERITS resolve, but only changed
    # files' nodes and edges touch the write lock. False forces a full rewrite.
    incremental: bool = True


class CodeIngestResponse(BaseModel):
    repos: int = 0
    modules: int = 0
    classes: int = 0
    functions: int = 0
    module_calls: int = 0  # TerraformModuleCall nodes (Terraform `module` blocks)
    edges: int = 0
    nodes_pruned: int = 0
    edges_pruned: int = 0
    # Incremental accounting: files parsed this run, and how many of them
    # were unchanged since their last ingest (their writes were skipped).
    files_parsed: int = 0
    files_unchanged: int = 0
    warnings: list[str] = Field(default_factory=list)


# --- UI -------------------------------------------------------------------------


class GraphStats(BaseModel):
    node_count: int = 0
    edge_count: int = 0
    labels: dict[str, int] = Field(default_factory=dict)


class GraphSample(BaseModel):
    subgraph: Subgraph = Field(default_factory=Subgraph)
    stats: GraphStats = Field(default_factory=GraphStats)
