"""Populate a grag knowledgebase with grag itself — dogfooding the LLM tool contract.

Builds ./knowledge.lbdb describing the grag project: its components, the design
decisions that shaped them, the concepts they implement, dependencies, the
MCP/CLI/REST tool surface, and the gotchas hit while building it. Then ingests
the README as searchable chunks and links chunks -> entities via MENTIONS.

Everything goes through GragService — the exact contract the MCP tools expose,
so this is also a live test of "can an LLM build its own knowledge graph".

    .venv/bin/python examples/build_self.py
    grag --db knowledge.lbdb serve
"""

from __future__ import annotations

import re
from pathlib import Path

from grag.config import GragConfig
from grag.core.types import (
    CodeIngestRequest,
    DefineSchemaRequest,
    IngestDocument,
    IngestRequest,
    NodeTableSpec,
    PropertySpec,
    QueryRequest,
    RelTableSpec,
    SearchRequest,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.service import GragService

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "knowledge.lbdb"
SRC = "examples/build_self.py"
SRC_README = "README.md"

S = lambda name, type="STRING": PropertySpec(name=name, type=type)  # noqa: E731


def define_schema(svc) -> None:
    svc.define_schema(
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(
                    name="Component",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("kind"), S("path"), S("summary")],
                ),
                NodeTableSpec(
                    name="Concept",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("summary")],
                ),
                NodeTableSpec(
                    name="Decision",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("summary"), S("rationale")],
                ),
                NodeTableSpec(
                    name="Dependency",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("version"), S("purpose")],
                ),
                NodeTableSpec(
                    name="Tool",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("kind"), S("summary")],
                ),
                NodeTableSpec(
                    name="Gotcha",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("summary"), S("fix")],
                ),
                NodeTableSpec(
                    name="Person",
                    primary_key="name",
                    searchable=True,
                    properties=[S("name"), S("role")],
                ),
            ],
            rel_tables=[
                RelTableSpec(
                    name="DEPENDS_ON",
                    from_label="Component",
                    to_label="Dependency",
                    properties=[S("scope")],
                ),
                RelTableSpec(
                    name="IMPLEMENTS", from_label="Component", to_label="Concept"
                ),
                RelTableSpec(name="EXPOSES", from_label="Component", to_label="Tool"),
                RelTableSpec(
                    name="INFORMS", from_label="Decision", to_label="Component"
                ),
                RelTableSpec(
                    name="APPLIES_TO", from_label="Gotcha", to_label="Component"
                ),
            ],
        )
    )


def define_mentions_schema(svc) -> None:
    """MENTIONS rels point at Chunk, which the ingest layer creates — so these
    tables can only be defined after ingestion has run."""
    svc.define_schema(
        DefineSchemaRequest(
            rel_tables=[
                RelTableSpec(
                    name="MENTIONS_COMPONENT", from_label="Chunk", to_label="Component"
                ),
                RelTableSpec(
                    name="MENTIONS_CONCEPT", from_label="Chunk", to_label="Concept"
                ),
                RelTableSpec(
                    name="MENTIONS_DECISION", from_label="Chunk", to_label="Decision"
                ),
                RelTableSpec(name="MENTIONS_TOOL", from_label="Chunk", to_label="Tool"),
            ],
        )
    )


def upsert_entities(svc) -> None:
    modules = [
        # name, kind, path, summary
        (
            "core.engine",
            "module",
            "src/grag/core/engine.py",
            "The only module that imports ladybug. Owns the embedded DB connection, a write "
            "lock plus read pool for thread safety, error wrapping into GragError with hints, "
            "FTS/VECTOR extension loading, and normalization of LadybugDB's internal node/rel "
            "values (uppercase _ID/_LABEL/_SRC/_DST keys) into plain Python types.",
        ),
        (
            "core.types",
            "module",
            "src/grag/core/types.py",
            "Frozen Pydantic contracts shared by every layer: graph primitives, schema specs, "
            "mutation/query/retrieval/ingest/UI payloads. Reserved _-prefixed props, vector "
            "prop names, META_TABLE registry, canonical 'Label:key' node ids.",
        ),
        (
            "core.errors",
            "module",
            "src/grag/core/errors.py",
            "GragError hierarchy where every error carries a message plus an optional hint, "
            "surfaced verbatim to LLM callers so they can self-correct in-loop.",
        ),
        (
            "core.ident",
            "module",
            "src/grag/core/ident.py",
            "Central identifier validation for every dynamic Cypher identifier. Rejects "
            "invalid labels, relationship types and properties before interpolation.",
        ),
        (
            "core.schema",
            "module",
            "src/grag/core/schema.py",
            "Schema introspection: SHOW_TABLES/TABLE_INFO plus the _grag_tables registry, "
            "pk_map (label -> primary key), build_schema_document (prompt-shaped schema text "
            "that anchors text-to-Cypher), and table_stats.",
        ),
        (
            "core.mutate",
            "module",
            "src/grag/core/mutate.py",
            "define_schema (DDL + registry bookkeeping) and idempotent upsert_nodes/"
            "upsert_edges built on MERGE semantics; provenance props (_source, _created_at) "
            "are attached automatically and _-prefixed props are rejected from callers.",
        ),
        (
            "core.serialize",
            "module",
            "src/grag/core/serialize.py",
            "pack_context: renders a subgraph into compact, cited, token-budgeted text for "
            "prompt injection; skips null-valued properties from mixed-label union rows.",
        ),
        (
            "config",
            "module",
            "src/grag/config.py",
            "GragConfig: db path, buffer pool size, query limits/hop caps, token budget and "
            "embedder/vector settings, multi-database routing, HTTP host/token controls and "
            "GRAG_* environment variable overrides.",
        ),
        (
            "registry",
            "module",
            "src/grag/registry.py",
            "ServiceRegistry owns one GragService per database and resolves safe short names "
            "under an optional multi-database root. REST and HTTP MCP share it.",
        ),
        (
            "service",
            "module",
            "src/grag/service.py",
            "Facade and single entry point for REST, MCP and CLI. Lazily delegates to core/"
            "retrieval/ingest modules, enforces read-only cypher_query (write keywords "
            "rejected), hides internal tables from results.",
        ),
        (
            "api.main",
            "module",
            "src/grag/api/main.py",
            "FastAPI app: /api/{query,search,context,ingest} POST, /api/{schema,graph/sample,"
            "health} GET, mutation POSTs, same-process MCP mounting, bearer protection, "
            "DNS-rebinding controls, and the built React UI with SPA fallback.",
        ),
        (
            "mcp_server",
            "module",
            "src/grag/mcp_server/server.py",
            "MCP stdio or streamable-HTTP server exposing seven knowledge tools plus "
            "ingest_code. HTTP supports multi-database routing and bearer auth; tool errors "
            "return 'ERROR: ... HINT: ...' so the model can retry with corrections.",
        ),
        (
            "proxy",
            "module",
            "src/grag/proxy.py",
            "Stdio-to-HTTP MCP proxy used by grag init/auto-serve. Verifies the live server's "
            "database fingerprint before attaching, preventing cross-project port reuse.",
        ),
        (
            "retrieval.search",
            "module",
            "src/grag/retrieval/search.py",
            "Hybrid retrieval: BM25 FTS seeds plus vector candidates fused with Reciprocal "
            "Rank Fusion, then k-hop graph expansion and token-budgeted context packing. "
            "Structure compensates for aggressive vector quantization.",
        ),
        (
            "retrieval.vectors",
            "module",
            "src/grag/retrieval/vectors.py",
            "Embedder interfaces (fastembed local, OpenAI-compatible remote), lazy vector "
            "storage via ALTER TABLE (embedding, _emb_r, _emb_code, _emb_model), pending-node "
            "embedding, pluggable codecs fp32/int8/binary/polar.",
        ),
        (
            "retrieval.polar",
            "module",
            "src/grag/retrieval/polar.py",
            "Experimental PolarQuant-style codec: polar decomposition stores magnitude r "
            "separately and quantizes direction angles with sine-power-law bit allocation, "
            "training-free. Codes generate candidates; exact fp32 rescore bounds recall loss.",
        ),
        (
            "retrieval.bench",
            "module",
            "src/grag/retrieval/bench.py",
            "Benchmark harness behind 'grag bench': recall@k, p50/p95 latency and RSS per "
            "codec on a synthetic corpus. Numbers land in the README codec table.",
        ),
        (
            "retrieval.context",
            "module",
            "src/grag/retrieval/context.py",
            "get_context: re-packs caller-chosen node ids (from a prior search) into a fresh "
            "token-budgeted context window.",
        ),
        (
            "ingest.loaders",
            "module",
            "src/grag/ingest/loaders.py",
            "Markdown/text/JSON/JSONL ingestion with overlap chunking and _source provenance. "
            "Canonical source hashes prevent basename collisions; authoritative re-ingest "
            "prunes stale chunks and reports the count.",
        ),
        (
            "ingest.code",
            "module",
            "src/grag/ingest/code.py",
            "Structural code ingestion into Repo/Module/Class/Function nodes and containment, "
            "import, inheritance and call edges. Path-hashed repo ids prevent collisions; "
            "re-indexing prunes removed files, symbols and generated edges.",
        ),
        (
            "ingest.code_ts",
            "module",
            "src/grag/ingest/code_ts.py",
            "Optional tree-sitter adapters for TypeScript, JavaScript, C# and Terraform, "
            "feeding the same language-neutral code graph as the Python AST parser.",
        ),
        (
            "cli",
            "module",
            "src/grag/cli.py",
            "argparse entry point with lazy subcommand imports: serve, mcp, ingest, "
            "ingest-code, bench, reindex and init.",
        ),
        (
            "ui",
            "ui",
            "ui/",
            "React 18 + TypeScript + Vite single-page explorer: force-graph canvas (click "
            "inspect, double-click expand), CodeMirror Cypher console, schema sidebar, and a "
            "search bar that shows the exact grounding text an LLM would receive. vite build "
            "outputs directly into src/grag/api/static and is served by FastAPI.",
        ),
    ]
    concepts = [
        (
            "hybrid search",
            "Seeds come from RRF fusion of BM25 full-text search and vector "
            "candidates; the graph then expands seeds k hops so answers include connected, "
            "cited context rather than isolated chunks.",
        ),
        (
            "polar decomposition",
            "Embeddings are split into magnitude r (one float) and "
            "unit direction u. Compressed codecs encode u for candidate generation; final "
            "scores use the stored fp32 embedding. The radius is retained but cosine retrieval "
            "does not currently consume it.",
        ),
        (
            "token-budgeted context packing",
            "Subgraphs serialize into compact text capped "
            "by a token budget, with [source: ...] citations on every node so LLM answers "
            "can be grounded and checked.",
        ),
        (
            "provenance",
            "Every table created via define_schema carries _source and "
            "_created_at; the mutation layer rejects caller writes to _-prefixed props.",
        ),
        (
            "schema introspection anchoring",
            "describe_schema renders tables, properties, "
            "row counts and sample keys as prompt-shaped text. Calling it before writing "
            "Cypher kills hallucinated labels and property names.",
        ),
        (
            "idempotent MERGE upserts",
            "upsert_nodes/upsert_edges use MERGE semantics so "
            "re-running a population pass updates in place instead of duplicating.",
        ),
        (
            "error hints for self-correction",
            "Errors are returned as 'ERROR: ... HINT: ...' "
            "so an LLM can fix its own request in the next tool call without human help.",
        ),
        (
            "single-writer embedded engine",
            "LadybugDB is embedded: one process holds the "
            "write lock per .lbdb file. Do not run 'grag serve' and 'grag mcp' against the "
            "same file concurrently.",
        ),
        (
            "LLM-built graphs",
            "define_schema + upsert tools are first-class, so 'turn these "
            "docs into a knowledge graph' is a conversation, not a pipeline project.",
        ),
        (
            "authoritative source synchronization",
            "Named document and code ingestion treat "
            "the latest successful parse as authoritative: removed chunks, symbols and generated "
            "edges are pruned while unchanged nodes retain stable identities.",
        ),
        (
            "collision-resistant source identity",
            "Document ids hash normalized source paths "
            "and repository ids hash canonical roots, so same-basename documents and checkouts "
            "cannot silently overwrite one another.",
        ),
        (
            "embedding freshness",
            "Upserts compare accepted searchable string properties. "
            "Unchanged text retains its embedding; changed text clears vector/model fields so "
            "the next embedding pass recomputes them.",
        ),
        (
            "local HTTP security",
            "Loopback is trusted by default. Remote standalone HTTP MCP "
            "fails closed without a bearer token; Host validation limits DNS rebinding and "
            "database/WAL files are owner-only on POSIX.",
        ),
    ]
    decisions = [
        (
            "LadybugDB as the engine",
            "Embedded columnar C++ graph engine, MIT licensed, "
            "successor to KuzuDB, native Cypher plus FTS and VECTOR extensions.",
            "One file per database, zero daemons, Cypher included, no JVM or enterprise "
            "platform. Rejected Neo4j-style client/server products as scope mismatch.",
        ),
        (
            "Python for the service layer",
            "Python 3.13 shell around the C++ engine: "
            "FastAPI REST, MCP stdio, Pydantic validation.",
            "Measured, not assumed: 16ms cold start, 1.6ms cypher round-trip, search p95 "
            "~18ms, ~339MB RSS. LLM tool-call latency is dominated by the model, not the "
            "shell, and the MCP reference SDK is Python. A Rust rewrite would buy nothing "
            "the agent loop can perceive.",
        ),
        (
            "Cypher as the query language",
            "One declarative graph query language for both the LLM and the UI console.",
            "LLMs already write Cypher fluently; inventing a new query language would burn "
            "tokens and invite hallucination.",
        ),
        (
            "fp32 as the safe default vector codec",
            "Codec ladder fp32/int8/binary/polar, "
            "selected by GRAG_VECTOR_CODEC; fp32 is the default and uses native HNSW.",
            "Compressed codecs use an O(rows) direction-code candidate scan followed by exact "
            "fp32 rescoring. int8 remains the best compressed tradeoff; polar is experimental.",
        ),
        (
            "Polar coordinates for RAG vectors",
            "Store magnitude and quantized direction instead of raw cartesian fp32.",
            "User-requested experiment: magnitude carries little retrieval signal, direction "
            "is what cosine similarity needs, so bits go to angles.",
        ),
        (
            "One process for API and UI",
            "vite build outputs into src/grag/api/static; "
            "FastAPI serves it with an SPA fallback.",
            "No separate frontend server, no CORS, one 'grag serve' command.",
        ),
        (
            "Errors carry hints",
            "Every GragError has message + hint, surfaced in REST 4xx "
            "bodies and MCP tool output.",
            "LLMs recover from mistakes when the error tells them how; this measurably "
            "reduces retry loops.",
        ),
        (
            "Fail closed for remote HTTP MCP",
            "Standalone streamable-HTTP MCP rejects a "
            "non-loopback bind unless GRAG_API_TOKEN is configured.",
            "Local-first defaults should remain frictionless, but network exposure must be an "
            "explicit authenticated decision.",
        ),
        (
            "Path-derived generated identities",
            "Generated document and repository keys "
            "include hashes of normalized/canonical source paths.",
            "Human-readable basenames alone collide across directories; stable path-derived "
            "keys preserve idempotency without cross-source overwrites.",
        ),
    ]
    dependencies = [
        (
            "ladybug",
            "0.19.1",
            "Embedded graph engine: storage, Cypher, BM25 FTS, vector index.",
        ),
        ("fastapi", "", "REST layer and static UI serving."),
        ("uvicorn", "", "ASGI server behind 'grag serve'; binds 127.0.0.1:8471."),
        (
            "pydantic",
            "",
            "Frozen data contracts; v2 validation runs in Rust (pydantic-core).",
        ),
        ("mcp", ">=2.0", "Model Context Protocol stdio server SDK."),
        ("numpy", "", "Vector codecs and polar quantization math."),
        ("react", "18", "UI: graph explorer, console, schema sidebar."),
        ("vite", "5", "UI build tool; outputs into src/grag/api/static."),
        (
            "fastembed",
            "",
            "Optional local embeddings (ONNX, no torch) via the embed-local extra.",
        ),
        (
            "tree-sitter",
            ">=0.23",
            "Optional structural parsing for TypeScript, JavaScript, C# and Terraform.",
        ),
    ]
    tools = [
        (
            "describe_schema",
            "mcp_tool",
            "Prompt-shaped schema with row counts and sample "
            "keys. Call before writing Cypher to avoid hallucinated labels.",
        ),
        (
            "define_schema",
            "mcp_tool",
            "Create node/rel tables; the LLM designs the graph "
            "for a domain. Records tables in the _grag_tables registry.",
        ),
        (
            "upsert_nodes",
            "mcp_tool",
            "Idempotent MERGE node writes with automatic "
            "_source/_created_at provenance.",
        ),
        (
            "upsert_edges",
            "mcp_tool",
            "Idempotent MERGE edge writes between existing nodes.",
        ),
        (
            "cypher_query",
            "mcp_tool",
            "Read-only Cypher with LIMIT clamping; write keywords "
            "are rejected with a hint pointing at the upsert tools.",
        ),
        (
            "search_knowledge",
            "mcp_tool",
            "Hybrid BM25 + vector seeds, RRF fusion, k-hop "
            "expansion, token-budgeted cited context.",
        ),
        (
            "get_context",
            "mcp_tool",
            "Re-pack chosen node ids from a prior search into a fresh token budget.",
        ),
        (
            "ingest_code",
            "mcp_tool",
            "Index code structure into Repo/Module/Class/Function "
            "nodes and synchronize removed symbols and generated edges.",
        ),
        (
            "serve",
            "cli",
            "Run FastAPI + the graph UI on 127.0.0.1:8471 against one .lbdb file.",
        ),
        ("mcp", "cli", "Run the MCP stdio server for Cursor or any MCP client."),
        (
            "ingest",
            "cli",
            "Chunk and load markdown/text/JSON/JSONL paths into Chunk nodes.",
        ),
        (
            "ingest-code",
            "cli",
            "Index Python and optional tree-sitter languages into the code graph.",
        ),
        ("bench", "cli", "Codec benchmark: recall@k, latency, RSS per vector codec."),
        (
            "reindex",
            "cli",
            "Rebuild embeddings and vector indexes after recovery or model changes.",
        ),
        (
            "init",
            "cli",
            "Register grag with supported LLM clients and write project guidance.",
        ),
    ]
    gotchas = [
        (
            "virtual memory exhaustion in tests",
            "LadybugDB reserves a huge virtual address "
            "space (~8.8TB) per open Database; many concurrent Engine instances made the "
            "kernel overcommit heuristic refuse mmaps.",
            "Engine.close() must close the ladybug.Database object, not just connections; "
            "test buffer pool raised to 128MB so FTS index creation fits.",
        ),
        (
            "uppercase internal keys",
            "LadybugDB returns node/rel values with uppercase "
            "internal keys (_ID, _LABEL, _SRC, _DST, _NODES, _RELS), not lowercase.",
            "engine value normalization reads the uppercase keys; smoke tests record the "
            "verified formats.",
        ),
        (
            "mixed-label union rows carry nulls",
            "A MATCH spanning several node labels "
            "returns a union schema with null for absent properties, polluting JSON payloads "
            "and the UI.",
            "node_record_from_value / edge_record_from_value drop None-valued properties.",
        ),
        (
            "vite relative base breaks SPA fallback",
            "base './' made asset URLs relative, so "
            "index.html served from a non-root path resolved /some/assets/... into the SPA "
            "fallback and the page rendered unstyled.",
            "base '/' in ui/vite.config.ts; assets always resolve from the origin root.",
        ),
        (
            "grid rows outnumber children",
            ".app defined 3 grid rows but only renders 2 "
            "children until a search opens the context panel, so .main landed in an auto row "
            "and content squished to the top.",
            "Explicit grid-row placement per child (topbar 1, context-panel 2, main 3).",
        ),
        (
            "QUERY_VECTOR_INDEX parameter types",
            "The vector extension expects the query "
            "vector as a plain Python list bound to a positional parameter; ::FLOAT[N] casts "
            "are not accepted in function arguments.",
            "Bind list[float] and int k as parameters; verified empirically and recorded in "
            "tests.",
        ),
        (
            "stale embeddings after text updates",
            "A MERGE update used to leave the previous "
            "embedding attached to changed searchable text.",
            "Compare accepted STRING values before the update and clear vector/model columns "
            "only when embedding input actually changes.",
        ),
        (
            "basename source collisions",
            "Documents or repositories with the same basename "
            "used to MERGE onto the same generated ids.",
            "Hash normalized document sources and canonical repository roots into generated "
            "identities, then prune legacy ids during authoritative re-ingestion.",
        ),
    ]
    people = [
        (
            "Adam",
            "Software engineer; creator of grag. Prefers working directly in "
            "code, iterating in the open.",
        )
    ]

    # NOTE: the primary key value is passed as `key`; it must NOT also appear in
    # `properties` — LadybugDB rejects SET on a primary-key column.
    nodes: list[UpsertNode] = []
    nodes += [
        UpsertNode(
            label="Component",
            key=n,
            source=SRC,
            properties={"kind": k, "path": p, "summary": s},
        )
        for n, k, p, s in modules
    ]
    nodes += [
        UpsertNode(label="Concept", key=n, source=SRC, properties={"summary": s})
        for n, s in concepts
    ]
    nodes += [
        UpsertNode(
            label="Decision",
            key=n,
            source=SRC,
            properties={"summary": s, "rationale": r},
        )
        for n, s, r in decisions
    ]
    nodes += [
        UpsertNode(
            label="Dependency",
            key=n,
            source=SRC,
            properties={"version": v, "purpose": p},
        )
        for n, v, p in dependencies
    ]
    nodes += [
        UpsertNode(
            label="Tool", key=n, source=SRC, properties={"kind": k, "summary": s}
        )
        for n, k, s in tools
    ]
    nodes += [
        UpsertNode(
            label="Gotcha", key=n, source=SRC, properties={"summary": s, "fix": f}
        )
        for n, s, f in gotchas
    ]
    nodes += [
        UpsertNode(label="Person", key=n, source=SRC, properties={"role": r})
        for n, r in people
    ]
    print("nodes:", svc.upsert_nodes(UpsertNodesRequest(nodes=nodes)))

    edges: list[UpsertEdge] = []

    def dep(mod, dep_name, scope="runtime"):
        edges.append(
            UpsertEdge(
                type="DEPENDS_ON",
                from_label="Component",
                from_key=mod,
                to_label="Dependency",
                to_key=dep_name,
                properties={"scope": scope},
                source=SRC,
            )
        )

    for m in [
        "core.engine",
        "core.schema",
        "core.mutate",
        "core.serialize",
        "retrieval.search",
        "retrieval.vectors",
        "retrieval.polar",
        "retrieval.bench",
        "retrieval.context",
        "ingest.loaders",
        "api.main",
        "mcp_server",
        "cli",
        "service",
        "core.types",
        "config",
    ]:
        dep(m, "ladybug" if m == "core.engine" else "pydantic")
    # fix the blanket loop above with precise scopes
    edges.clear()
    dep("core.engine", "ladybug")
    dep("api.main", "fastapi")
    dep("api.main", "uvicorn")
    dep("mcp_server", "mcp")
    dep("retrieval.polar", "numpy")
    dep("retrieval.vectors", "numpy")
    dep("retrieval.vectors", "fastembed", scope="optional:embed-local")
    dep("retrieval.bench", "numpy")
    dep("ingest.code_ts", "tree-sitter", scope="optional:code")
    dep("ui", "react")
    dep("ui", "vite", scope="dev")
    for m in [
        "service",
        "registry",
        "core.types",
        "config",
        "core.schema",
        "core.mutate",
        "core.serialize",
        "ingest.loaders",
        "ingest.code",
        "retrieval.search",
        "retrieval.context",
        "cli",
    ]:
        dep(m, "pydantic")

    def impl(mod, concept):
        edges.append(
            UpsertEdge(
                type="IMPLEMENTS",
                from_label="Component",
                from_key=mod,
                to_label="Concept",
                to_key=concept,
                source=SRC,
            )
        )

    impl("retrieval.search", "hybrid search")
    impl("retrieval.polar", "polar decomposition")
    impl("retrieval.vectors", "polar decomposition")
    impl("core.serialize", "token-budgeted context packing")
    impl("retrieval.context", "token-budgeted context packing")
    impl("core.mutate", "provenance")
    impl("core.mutate", "idempotent MERGE upserts")
    impl("core.schema", "schema introspection anchoring")
    impl("core.errors", "error hints for self-correction")
    impl("mcp_server", "error hints for self-correction")
    impl("core.engine", "single-writer embedded engine")
    impl("mcp_server", "LLM-built graphs")
    impl("core.mutate", "LLM-built graphs")
    impl("ingest.loaders", "authoritative source synchronization")
    impl("ingest.code", "authoritative source synchronization")
    impl("ingest.loaders", "collision-resistant source identity")
    impl("ingest.code", "collision-resistant source identity")
    impl("core.mutate", "embedding freshness")
    impl("mcp_server", "local HTTP security")
    impl("api.main", "local HTTP security")
    impl("core.engine", "local HTTP security")

    def exposes(mod, tool):
        edges.append(
            UpsertEdge(
                type="EXPOSES",
                from_label="Component",
                from_key=mod,
                to_label="Tool",
                to_key=tool,
                source=SRC,
            )
        )

    for t in [
        "describe_schema",
        "define_schema",
        "upsert_nodes",
        "upsert_edges",
        "cypher_query",
        "search_knowledge",
        "get_context",
        "ingest_code",
    ]:
        exposes("mcp_server", t)
        exposes("service", t)
    for t in ["serve", "mcp", "ingest", "ingest-code", "bench", "reindex", "init"]:
        exposes("cli", t)
    exposes("api.main", "search_knowledge")
    exposes("api.main", "cypher_query")
    exposes("api.main", "describe_schema")
    exposes("api.main", "get_context")

    def informs(decision, mod):
        edges.append(
            UpsertEdge(
                type="INFORMS",
                from_label="Decision",
                from_key=decision,
                to_label="Component",
                to_key=mod,
                source=SRC,
            )
        )

    informs("LadybugDB as the engine", "core.engine")
    informs("Python for the service layer", "service")
    informs("Python for the service layer", "api.main")
    informs("Python for the service layer", "mcp_server")
    informs("Cypher as the query language", "core.engine")
    informs("Cypher as the query language", "mcp_server")
    informs("fp32 as the safe default vector codec", "retrieval.vectors")
    informs("fp32 as the safe default vector codec", "retrieval.bench")
    informs("Polar coordinates for RAG vectors", "retrieval.polar")
    informs("One process for API and UI", "api.main")
    informs("One process for API and UI", "ui")
    informs("Errors carry hints", "core.errors")
    informs("Errors carry hints", "mcp_server")
    informs("Fail closed for remote HTTP MCP", "mcp_server")
    informs("Path-derived generated identities", "ingest.loaders")
    informs("Path-derived generated identities", "ingest.code")

    def applies(gotcha, mod):
        edges.append(
            UpsertEdge(
                type="APPLIES_TO",
                from_label="Gotcha",
                from_key=gotcha,
                to_label="Component",
                to_key=mod,
                source=SRC,
            )
        )

    applies("virtual memory exhaustion in tests", "core.engine")
    applies("uppercase internal keys", "core.engine")
    applies("mixed-label union rows carry nulls", "core.engine")
    applies("mixed-label union rows carry nulls", "core.serialize")
    applies("vite relative base breaks SPA fallback", "ui")
    applies("grid rows outnumber children", "ui")
    applies("QUERY_VECTOR_INDEX parameter types", "retrieval.vectors")
    applies("stale embeddings after text updates", "core.mutate")
    applies("stale embeddings after text updates", "retrieval.vectors")
    applies("basename source collisions", "ingest.loaders")
    applies("basename source collisions", "ingest.code")

    print("edges:", svc.upsert_edges(UpsertEdgesRequest(edges=edges)))


def ingest_readme(svc) -> None:
    text = (ROOT / "README.md").read_text()
    res = svc.ingest(
        IngestRequest(
            documents=[IngestDocument(text=text, source=SRC_README)],
            chunk=True,
        )
    )
    print("ingest:", res)


def ingest_own_code(svc) -> None:
    """Index grag's Python package, then connect curated components to code modules."""

    result = svc.ingest_code(
        CodeIngestRequest(paths=[str(ROOT / "src" / "grag")], calls=True)
    )
    print("code:", result)
    svc.define_schema(
        DefineSchemaRequest(
            rel_tables=[
                RelTableSpec(
                    name="MAPS_TO_CODE",
                    from_label="Component",
                    to_label="Module",
                )
            ]
        )
    )
    components = svc.cypher_query(
        QueryRequest(cypher="MATCH (c:Component) RETURN c.name, c.path", limit=500)
    ).rows
    modules = svc.cypher_query(
        QueryRequest(cypher="MATCH (m:Module) RETURN m.id, m.path", limit=500)
    ).rows
    module_by_path = {f"src/grag/{path}": module_id for module_id, path in modules}
    links = [
        UpsertEdge(
            type="MAPS_TO_CODE",
            from_label="Component",
            from_key=name,
            to_label="Module",
            to_key=module_by_path[path],
            source=SRC,
        )
        for name, path in components
        if path in module_by_path
    ]
    print("component-code links:", svc.upsert_edges(UpsertEdgesRequest(edges=links)))


def link_mentions(svc) -> None:
    """Connect README chunks to the entities they mention (Chunk -> Component/Concept/
    Decision/Tool) via the MENTIONS rel table."""
    rows = svc.cypher_query(
        QueryRequest(
            cypher="MATCH (c:Chunk) RETURN c.id AS id, c.text AS text", limit=500
        )
    ).rows
    patterns: list[tuple[str, str, re.Pattern]] = []

    def add(label, key, *aliases):
        pats = [re.escape(key)] + [re.escape(a) for a in aliases]
        patterns.append((label, key, re.compile(r"(?i)\b(" + "|".join(pats) + r")\b")))

    for key, path in [
        ("core.engine", "engine.py"),
        ("core.types", "types.py"),
        ("core.ident", "identifier validation"),
        ("core.mutate", "mutate"),
        ("core.schema", "schema introspection"),
        ("core.serialize", "serialize"),
        ("service", "GragService"),
        ("registry", "ServiceRegistry"),
        ("api.main", "FastAPI"),
        ("mcp_server", "mcp_server"),
        ("proxy", "auto-serve"),
        ("retrieval.search", "retrieval"),
        ("retrieval.vectors", "vectors"),
        ("retrieval.polar", "polar.py"),
        ("retrieval.bench", "bench"),
        ("ingest.loaders", "loaders"),
        ("ingest.code", "ingest_code"),
        ("ingest.code_ts", "tree-sitter"),
        ("ui", "graph explorer"),
        ("cli", "cli"),
    ]:
        add("Component", key, path)
    for key, *aliases in [
        ("hybrid search", "hybrid"),
        ("polar decomposition", "polar"),
        ("token-budgeted context packing", "token-budgeted"),
        ("provenance", "_source"),
        ("schema introspection anchoring", "describe_schema"),
        ("idempotent MERGE upserts", "MERGE"),
        ("error hints for self-correction", "HINT"),
        ("single-writer embedded engine", "single-writer", "embedded"),
        ("LLM-built graphs",),
        ("authoritative source synchronization", "stale", "pruned"),
        ("collision-resistant source identity", "path-hash", "collision"),
        ("embedding freshness", "pending_embeddings"),
        ("local HTTP security", "bearer", "GRAG_API_TOKEN"),
    ]:
        add("Concept", key, *aliases)
    for key in [
        "LadybugDB as the engine",
        "Python for the service layer",
        "Cypher as the query language",
        "fp32 as the safe default vector codec",
        "Polar coordinates for RAG vectors",
        "One process for API and UI",
        "Errors carry hints",
        "Fail closed for remote HTTP MCP",
        "Path-derived generated identities",
    ]:
        add("Decision", key)
    for key in [
        "describe_schema",
        "define_schema",
        "upsert_nodes",
        "upsert_edges",
        "cypher_query",
        "search_knowledge",
        "get_context",
        "serve",
        "mcp",
        "ingest",
        "ingest_code",
        "ingest-code",
        "bench",
        "reindex",
        "init",
    ]:
        add("Tool", key)

    rel_type = {
        "Component": "MENTIONS_COMPONENT",
        "Concept": "MENTIONS_CONCEPT",
        "Decision": "MENTIONS_DECISION",
        "Tool": "MENTIONS_TOOL",
    }
    edges: list[UpsertEdge] = []
    for chunk_id, text in rows:
        for label, key, pat in patterns:
            if pat.search(text or ""):
                edges.append(
                    UpsertEdge(
                        type=rel_type[label],
                        from_label="Chunk",
                        from_key=chunk_id,
                        to_label=label,
                        to_key=key,
                        source=SRC,
                    )
                )
    print("mentions:", svc.upsert_edges(UpsertEdgesRequest(edges=edges)))


def verify(svc) -> None:
    print("\n=== schema ===")
    print(svc.describe_schema().text)
    print("\n=== sanity searches ===")
    for q in [
        "why is the backend python",
        "which vector codec should I use",
        "how does ingestion prune stale nodes",
        "how is remote HTTP protected",
        "mmap failure",
        "what tools does the MCP server expose",
    ]:
        res = svc.search_knowledge(SearchRequest(query=q, top_k=3, hops=1))
        print(f"\nQ: {q}")
        for s in res.seeds[:3]:
            print(f"  [{s.match} {s.score:.3f}] {s.node.id}")


def main() -> None:
    if DB.exists():
        DB.unlink()
        wal = DB.with_suffix(DB.suffix + ".wal")
        if wal.exists():
            wal.unlink()
    svc = GragService(GragConfig(db_path=DB))
    try:
        define_schema(svc)
        upsert_entities(svc)
        ingest_own_code(svc)
        ingest_readme(svc)
        define_mentions_schema(svc)
        link_mentions(svc)
        verify(svc)
    finally:
        svc.close()
    print(f"\nBuilt {DB}. Browse it: grag --db knowledge.lbdb serve")


if __name__ == "__main__":
    main()
