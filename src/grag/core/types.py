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
    ingest_code(req, background=true) POST /api/jobs/ingest/code -> JobRecord (202)
    (ingest, background)              POST /api/jobs/ingest    -> JobRecord (202)
    job_status(job_id)                GET  /api/jobs/{id}      -> JobRecord
    (jobs)                            GET  /api/jobs           -> {"jobs": [JobRecord]}
    (backup)                          GET  /api/export         -> JSONL stream (grag export)
    (ui)                              GET  /api/graph/sample   -> GraphSample
    (ui)                              GET  /api/graph/full     -> GraphSample
    (freshness)                       GET  /api/index/status   -> per-root generations/errors
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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- reserved property conventions ---------------------------------------------

RESERVED_PREFIX = "_"
PROVENANCE_SOURCE = "_source"  # STRING: origin of a fact (file, url, doc id)
PROVENANCE_CREATED_AT = "_created_at"  # TIMESTAMP: write time
DOCUMENT_OWNER_PROP = "_document_owner"  # STRING: loader-owned edge's document id

EMBEDDING_PROP = "embedding"  # FLOAT[dim]: exact vector, rescore source of truth
EMB_MAGNITUDE_PROP = "_emb_r"  # DOUBLE: ||v|| at write time (polar split)
EMB_CODE_PROP = "_emb_code"  # UINT8[]: quantized direction codes
EMB_MODEL_PROP = "_emb_model"  # STRING: embedder model id
EMB_FINGERPRINT_PROP = "_emb_fingerprint"  # STRING: embedding configuration identity

VECTOR_PROPS = {
    EMBEDDING_PROP, EMB_MAGNITUDE_PROP, EMB_CODE_PROP, EMB_MODEL_PROP, EMB_FINGERPRINT_PROP,
}

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
    model_config = ConfigDict(extra="forbid")
    name: str
    type: CypherType = "STRING"


class NodeTableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    primary_key: str = "id"
    properties: list[PropertySpec] = Field(default_factory=list)
    searchable: bool = True  # retrieval layer maintains an FTS index on STRING props


class RelTableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    from_label: str
    to_label: str
    properties: list[PropertySpec] = Field(default_factory=list)


class DefineSchemaRequest(BaseModel):
    node_tables: list[NodeTableSpec] = Field(default_factory=list)
    rel_tables: list[RelTableSpec] = Field(default_factory=list)
    if_not_exists: bool = True
    # A new table whose name is a near-duplicate of an existing one (case,
    # plural, punctuation: "Decisions" vs "Decision") is refused with a hint
    # to reuse it — the main way agent-built graphs fragment. Set True to
    # create it anyway.
    allow_similar: bool = False


class PropertyDoc(BaseModel):
    name: str
    type: str
    is_primary_key: bool = False


class NodeTableDoc(BaseModel):
    name: str
    properties: list[PropertyDoc] = Field(default_factory=list)
    row_count: int | None = None  # unknown when detail=compact
    sample_keys: list[str] = Field(default_factory=list)
    searchable: bool = False


class RelTableDoc(BaseModel):
    name: str
    from_label: str = ""
    to_label: str = ""
    properties: list[PropertyDoc] = Field(default_factory=list)
    row_count: int | None = None


FreshnessMode = Literal["allow_stale", "wait", "require"]
FreshnessState = Literal["fresh", "checking", "refreshing", "stale", "error", "unknown", "disabled"]


class ReadPolicy(BaseModel):
    freshness: FreshnessMode = "allow_stale"
    freshness_timeout_ms: int = Field(default=5000, ge=0, le=60_000)


class FreshnessReport(BaseModel):
    """Code-index verification, as of checked_at; not a filesystem snapshot."""

    status: FreshnessState = "disabled"
    checked_at: str | None = None
    timed_out: bool = False


SchemaDetail = Literal["compact", "full"]


class SchemaDocument(BaseModel):
    """Full schema introspection. `text` is the prompt-shaped rendering an LLM
    anchors on before writing Cypher — keep it compact."""

    node_tables: list[NodeTableDoc] = Field(default_factory=list)
    rel_tables: list[RelTableDoc] = Field(default_factory=list)
    text: str = ""
    detail: SchemaDetail = "full"
    schema_revision: str = ""
    unchanged: bool = False
    freshness: FreshnessReport = Field(default_factory=FreshnessReport)


# --- mutation -------------------------------------------------------------------


class EvidenceUpdate(BaseModel):
    """Opt a memory into history, or patch its explicit evidence lifecycle."""

    model_config = ConfigDict(extra="forbid")
    state: Literal["current", "superseded", "retracted"] | None = None
    review: Literal["unreviewed", "accepted", "disputed"] | None = None
    expires_at: datetime | None = None
    superseded_by: str | None = Field(default=None, min_length=3, max_length=2048)
    actor: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def explicit_values(self) -> EvidenceUpdate:
        for field in ("state", "review"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("expires_at requires an explicit timezone")
        return self


class UpsertNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    key: Any  # primary key value
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None  # provenance -> _source
    expected_revision: str | None = Field(default=None, pattern=r"^(absent|[0-9a-f]{64})$")
    evidence: EvidenceUpdate | None = None


class UpsertEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    from_label: str
    from_key: Any
    to_label: str
    to_key: Any
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    expected_revision: str | None = Field(default=None, pattern=r"^(absent|[0-9a-f]{64})$")


class UpsertNodesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[UpsertNode]
    edges: list[UpsertEdge] = Field(default_factory=list)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128, strict=True)


class UpsertEdgesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edges: list[UpsertEdge]
    operation_id: str | None = Field(default=None, min_length=1, max_length=128, strict=True)


class MutationSummary(BaseModel):
    nodes: int = 0
    edges: int = 0
    warnings: list[str] = Field(default_factory=list)
    operation_id: str | None = None
    replayed: bool = False
    revisions: dict[str, str] = Field(default_factory=dict)


# --- query ----------------------------------------------------------------------


class QueryRequest(ReadPolicy):
    cypher: str = Field(max_length=65_536)
    limit: int | None = None  # clamped to [1, config.max_query_limit]


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    subgraph: Subgraph = Field(default_factory=Subgraph)
    freshness: FreshnessReport = Field(default_factory=FreshnessReport)


# --- retrieval ------------------------------------------------------------------

# A successful response needs room for its graph and completeness metadata.
MIN_RETRIEVAL_BUDGET = 256
EvidenceMode = Literal["current", "all"]


class SearchRequest(ReadPolicy):
    query: str = Field(max_length=8192)
    top_k: int = Field(default=8, ge=1, le=64)
    hops: int = 1  # clamped to config.max_hops
    labels: list[str] | None = Field(default=None, max_length=64)  # seed tables
    token_budget: int | None = Field(default=None, ge=MIN_RETRIEVAL_BUDGET, le=32_768)
    evidence: EvidenceMode = "current"


class ScoredNode(BaseModel):
    node: NodeRecord
    score: float
    match: Literal["fts", "vector", "graph"]


class TextExcerpt(BaseModel):
    """Exact partial STRING evidence; offsets are Unicode character positions."""

    node_id: str
    property: str
    text: str
    offset: int
    end: int  # exclusive
    total_chars: int
    sha256: str  # compatible with get_context(text_sha256=...)


class RetrievalMetadata(BaseModel):
    token_estimate: int = 0  # context text only, ceil(UTF-8 bytes / 4)
    response_token_estimate: int = 0  # larger of compact JSON and MCP text
    included_node_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    omitted_nodes: int = 0
    omitted_edges: int = 0
    omitted_properties: int = 0  # on included records; values are never clipped
    expansion_limited: bool = False  # path enumeration reached its cap
    freshness: FreshnessReport = Field(default_factory=FreshnessReport)
    text_excerpts: list[TextExcerpt] = Field(default_factory=list)
    evidence_policy: EvidenceMode | None = None
    excluded_evidence: int = 0  # encountered nodes excluded, not a graph-wide count


class SearchResponse(RetrievalMetadata):
    seeds: list[ScoredNode] = Field(default_factory=list)
    subgraph: Subgraph = Field(default_factory=Subgraph)  # budgeted seeds + expansion
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
    # "refreshing" when the serving process detected that an indexed checkout
    # changed (new commit, edited files) and queued an incremental re-ingest;
    # this answer came from the graph as it was, the next one sees the update.
    index_status: Literal["refreshing"] | None = None


class ContextRequest(ReadPolicy):
    node_ids: list[str] = Field(max_length=64)
    hops: int = 1
    token_budget: int | None = Field(default=None, ge=MIN_RETRIEVAL_BUDGET, le=32_768)
    text_property: str | None = None  # page one STRING property on one node
    text_offset: int = Field(default=0, ge=0)  # Unicode character offset
    text_sha256: str | None = None  # refuse continuation if the text changed
    evidence: EvidenceMode = "current"
    history: bool = False
    history_before: int | None = Field(default=None, ge=1, le=2**63-1)
    revision: int | None = Field(default=None, ge=0, le=2**63-1)

    @model_validator(mode="after")
    def validate_text_page(self) -> ContextRequest:
        if (self.history or self.revision is not None) and len(self.node_ids) != 1:
            raise ValueError("history/revision requires exactly one node id")
        if self.history_before is not None and not self.history:
            raise ValueError("history_before requires history=true")
        if self.history and (self.revision is not None or self.text_property is not None):
            raise ValueError("history cannot be combined with revision or text paging")
        if self.text_property is not None:
            if not self.text_property or len(self.node_ids) != 1:
                raise ValueError(
                    "text_property requires exactly one node id and a property name"
                )
        elif self.text_offset or self.text_sha256 is not None:
            raise ValueError("text_offset/text_sha256 require text_property")
        return self


class TextPage(BaseModel):
    node_id: str
    property: str
    offset: int
    next_offset: int | None  # None at end; otherwise pass to text_offset
    total_chars: int
    sha256: str  # pass to text_sha256 when continuing


class ContextResponse(RetrievalMetadata):
    context: str
    subgraph: Subgraph = Field(default_factory=Subgraph)
    text_page: TextPage | None = None
    history: EvidenceHistory | None = None


class EvidenceHistoryEntry(BaseModel):
    sequence: int
    revision: str
    recorded_at: str
    source: str | None = None
    actor: str | None = None
    reason: str | None = None
    baseline: bool = False


class EvidenceHistory(BaseModel):
    node_id: str
    entries: list[EvidenceHistoryEntry] = Field(default_factory=list)
    next_before: int | None = None


class PackedContext(BaseModel):
    """Internal result of serialize.pack_context."""

    text: str
    token_estimate: int
    included_node_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    omitted_nodes: int = 0
    omitted_edges: int = 0
    omitted_properties: int = 0
    subgraph: Subgraph = Field(default_factory=Subgraph)
    text_excerpts: list[TextExcerpt] = Field(default_factory=list)


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
    # Section-aware mode (grag.ingest.markdown): parse the heading hierarchy
    # into Document/Section nodes, chunk each section's body under it, and
    # link backtick-mentioned code symbols to the code graph. False keeps the
    # flat chunk loader.
    sections: bool = False


class IngestResponse(BaseModel):
    label: str
    nodes_created: int  # chunk nodes written
    nodes_pruned: int = 0
    documents: int = 0  # sections mode: Document nodes written
    sections: int = 0  # sections mode: Section nodes written
    code_links: int = 0  # sections mode: MENTIONS_* edges to code symbols
    warnings: list[str] = Field(default_factory=list)


class CodeIngestRequest(BaseModel):
    paths: list[str]
    calls: bool = True
    max_file_kb: int = Field(default=1024, ge=1, le=32_768)
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


# --- background jobs ----------------------------------------------------------------

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class JobRecord(BaseModel):
    """One background ingest run (POST /api/jobs/...). Kept in memory for the
    life of the serving process; ``result`` is the ingest response model dump."""

    id: str
    kind: str  # "ingest_code" | "ingest"
    status: JobStatus = "queued"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None


# --- UI -------------------------------------------------------------------------


class GraphStats(BaseModel):
    node_count: int = 0
    edge_count: int = 0
    labels: dict[str, int] = Field(default_factory=dict)


class GraphSample(BaseModel):
    subgraph: Subgraph = Field(default_factory=Subgraph)
    stats: GraphStats = Field(default_factory=GraphStats)
    freshness: FreshnessReport = Field(default_factory=FreshnessReport)
