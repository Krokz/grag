"""Build examples/knowledge.lbdb from scratch via the public grag API.

Creates a richer schema than plain ingestion (Chunk + Entity, linked by
MENTIONS and RELATES edges), ingests the Meridian Labs corpus from
examples/corpus/, scans chunk text for entity mentions, and adds a dozen
hand-written RELATES edges tying the domain together. Rerunnable: any
pre-existing examples/knowledge.lbdb is deleted first.

Run from the repo root:

    .venv/bin/python examples/build_example.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from grag.config import GragConfig
from grag.core.types import (
    DefineSchemaRequest,
    IngestDocument,
    IngestRequest,
    NodeTableSpec,
    PropertySpec,
    QueryRequest,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.service import GragService

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
DB_PATH = ROOT / "knowledge.lbdb"

# Entity catalogue: (name, kind, one-line summary). Names appear verbatim in
# the corpus, which is what makes the MENTIONS scan and FTS work.
ENTITIES: list[tuple[str, str, str]] = [
    # products
    ("Pulse", "product", "Metrics platform; flagship product, being rebuilt as Pulse 2.0 (Project Aurora)."),
    ("Tracepath", "product", "Distributed tracing product; Acme Analytics is its largest user."),
    ("Beacon", "product", "Alerting product backed by the alert-engine; Copperline Retail pilot."),
    # services
    ("ingestion-gateway", "service", "Rust ingest front door (ADR-014), owned by Tom Okafor. Caused the March outage."),
    ("query-service", "service", "Reads Coldstore and serves Pulse/Tracepath APIs; owned by Priya Raman."),
    ("alert-engine", "service", "Evaluates Beacon alert rules over Pulse metrics; owned by Lena Fischer."),
    ("Coldstore", "service", "Columnar storage cluster (ADR-009) operated by Sam Whitfield's SRE team."),
    # people
    ("Mara Voss", "person", "CEO and co-founder; ran the Acme Analytics apology call after the March outage."),
    ("Deniz Aydin", "person", "CTO and co-founder; sponsors Project Aurora and Project Bedrock, approves ADRs."),
    ("Priya Raman", "person", "Engineering lead for Pulse; owns the query-service; tech lead of Project Aurora."),
    ("Tom Okafor", "person", "Engineering lead for data ingress; owns the ingestion-gateway and Tracepath pipeline."),
    ("Lena Fischer", "person", "Engineering lead for alerting; owns the alert-engine and Beacon."),
    ("Sam Whitfield", "person", "SRE lead; owns Coldstore, wrote the March outage postmortem, leads Project Bedrock."),
    # projects
    ("Project Aurora", "project", "Pulse 2.0: query-service rewrite targeting p95 < 200 ms dashboards."),
    ("Project Bedrock", "project", "Coldstore v2: columnar re-architecture plus backup/restore, led by Sam Whitfield."),
    # customers
    ("Acme Analytics", "customer", "Largest customer; runs Pulse and Tracepath; worst hit by the March outage."),
    ("Bluefin Health", "customer", "Pulse customer with a dedicated Coldstore namespace; cold-path reference account."),
    ("Copperline Retail", "customer", "Running a Beacon pilot over their Pulse metrics."),
    # incident + decision
    ("March outage", "incident", "2026-03-14: bad global config deploy to the ingestion-gateway dropped ~30% of batches."),
    ("ADR-017", "decision", "Mandatory 5% canary deploys for the ingestion-gateway and alert-engine."),
]

# Hand-written edges tying the corpus together: (from, to, note).
RELATES: list[tuple[str, str, str]] = [
    ("Priya Raman", "query-service", "owns"),
    ("Tom Okafor", "ingestion-gateway", "owns"),
    ("Lena Fischer", "alert-engine", "owns"),
    ("Sam Whitfield", "Coldstore", "operates"),
    ("Deniz Aydin", "Project Aurora", "sponsors"),
    ("Priya Raman", "Project Aurora", "tech lead"),
    ("Sam Whitfield", "Project Bedrock", "leads"),
    ("Project Aurora", "Pulse", "rebuilds as Pulse 2.0"),
    ("Project Bedrock", "Coldstore", "re-architects (ADR-009)"),
    ("March outage", "ingestion-gateway", "caused by bad global config deploy"),
    ("March outage", "Acme Analytics", "lost four hours of telemetry"),
    ("ADR-017", "March outage", "adopted in response to"),
    ("ADR-017", "ingestion-gateway", "requires 5% canary deploys"),
    ("Beacon", "alert-engine", "rules evaluated by"),
    ("Pulse", "query-service", "dashboards served by"),
    ("Acme Analytics", "Tracepath", "largest user since 2025"),
    ("Copperline Retail", "Beacon", "running a pilot"),
]


def main() -> int:
    for stale in DB_PATH.parent.glob(DB_PATH.name + "*"):
        stale.unlink()  # db file + WAL: rebuild from scratch every run

    service = GragService(GragConfig(db_path=DB_PATH))
    try:
        doc = service.define_schema(
            DefineSchemaRequest(
                node_tables=[
                    NodeTableSpec(
                        name="Chunk",
                        primary_key="id",
                        properties=[PropertySpec(name="text"), PropertySpec(name="meta")],
                        searchable=True,
                    ),
                    NodeTableSpec(
                        name="Entity",
                        primary_key="name",
                        properties=[PropertySpec(name="kind"), PropertySpec(name="summary")],
                        searchable=True,
                    ),
                ],
                rel_tables=[
                    RelTableSpec(name="MENTIONS", from_label="Chunk", to_label="Entity"),
                    RelTableSpec(
                        name="RELATES",
                        from_label="Entity",
                        to_label="Entity",
                        properties=[PropertySpec(name="note")],
                    ),
                ],
            )
        )

        documents = [
            IngestDocument(text=p.read_text(encoding="utf-8"), source=str(p))
            for p in sorted(CORPUS.glob("*.md"))
        ]
        ing = service.ingest(
            IngestRequest(documents=documents, label="Chunk", chunk_size=900, chunk_overlap=120)
        )

        ent = service.upsert_nodes(
            UpsertNodesRequest(
                nodes=[
                    UpsertNode(
                        label="Entity",
                        key=name,
                        properties={"kind": kind, "summary": summary},
                        source="examples/build_example.py",
                    )
                    for name, kind, summary in ENTITIES
                ]
            )
        )

        # MENTIONS: link every chunk to the entities named in its text.
        chunks = service.cypher_query(
            QueryRequest(cypher="MATCH (c:Chunk) RETURN c.id, c.text", limit=1000)
        )
        mentions = [
            UpsertEdge(
                type="MENTIONS",
                from_label="Chunk",
                from_key=chunk_id,
                to_label="Entity",
                to_key=name,
                source="examples/build_example.py",
            )
            for chunk_id, text in chunks.rows
            for name, _, _ in ENTITIES
            if name.lower() in str(text).lower()
        ]
        men = service.upsert_edges(UpsertEdgesRequest(edges=mentions))

        rel = service.upsert_edges(
            UpsertEdgesRequest(
                edges=[
                    UpsertEdge(
                        type="RELATES",
                        from_label="Entity",
                        from_key=src,
                        to_label="Entity",
                        to_key=dst,
                        properties={"note": note},
                        source="examples/build_example.py",
                    )
                    for src, dst, note in RELATES
                ]
            )
        )

        stats = service.describe_schema()
        print(f"Built {DB_PATH}")
        print(f"  documents ingested : {len(documents)} -> {ing.nodes_created} chunks")
        print(f"  entities           : {ent.nodes}")
        print(f"  MENTIONS edges     : {men.edges}")
        print(f"  RELATES edges      : {rel.edges}")
        print("\nSchema:\n" + stats.text)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
