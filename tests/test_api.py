"""End-to-end tests for the REST API (grag.api.main.create_app)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import grag
from grag.api.main import _STATIC_DIR, create_app
from grag.config import GragConfig
from grag.core.types import (
    ContextResponse,
    GraphSample,
    IngestResponse,
    MutationSummary,
    QueryResponse,
    SchemaDocument,
    SearchResponse,
)


def _static_built() -> bool:
    return (_STATIC_DIR / "index.html").is_file()


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        GragConfig(db_path=tmp_path / "api.lbdb", buffer_pool_size=128 * 1024 * 1024)
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded_client(client):
    """Schema (Person/KNOWS) + two nodes + one edge."""
    schema = client.post(
        "/api/schema/define",
        json={
            "node_tables": [
                {
                    "name": "Person",
                    "primary_key": "id",
                    "properties": [
                        {"name": "id", "type": "STRING"},
                        {"name": "name", "type": "STRING"},
                    ],
                }
            ],
            "rel_tables": [
                {"name": "KNOWS", "from_label": "Person", "to_label": "Person"}
            ],
        },
    )
    assert schema.status_code == 200
    nodes = client.post(
        "/api/nodes/upsert",
        json={
            "nodes": [
                {"label": "Person", "key": "alice", "properties": {"name": "Alice"}},
                {"label": "Person", "key": "bob", "properties": {"name": "Bob"}},
            ]
        },
    )
    assert nodes.status_code == 200
    edges = client.post(
        "/api/edges/upsert",
        json={
            "edges": [
                {
                    "type": "KNOWS",
                    "from_label": "Person",
                    "from_key": "alice",
                    "to_label": "Person",
                    "to_key": "bob",
                }
            ]
        },
    )
    assert edges.status_code == 200
    return client


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "version": grag.__version__}


def test_define_schema_and_roundtrip(client):
    res = client.post(
        "/api/schema/define",
        json={
            "node_tables": [
                {
                    "name": "Doc",
                    "primary_key": "id",
                    "properties": [
                        {"name": "id", "type": "STRING"},
                        {"name": "title", "type": "STRING"},
                    ],
                }
            ]
        },
    )
    assert res.status_code == 200
    doc = SchemaDocument.model_validate(res.json())
    assert any(t.name == "Doc" for t in doc.node_tables)

    got = client.get("/api/schema")
    assert got.status_code == 200
    doc2 = SchemaDocument.model_validate(got.json())
    assert any(t.name == "Doc" for t in doc2.node_tables)

    text = client.get("/api/schema", params={"format": "text"})
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert "Doc(" in text.text


def test_upsert_nodes_and_edges(seeded_client):
    res = seeded_client.post(
        "/api/nodes/upsert",
        json={"nodes": [{"label": "Person", "key": "carol", "properties": {"name": "Carol"}}]},
    )
    assert res.status_code == 200
    summary = MutationSummary.model_validate(res.json())
    assert summary.nodes == 1

    res = seeded_client.post(
        "/api/edges/upsert",
        json={
            "edges": [
                {
                    "type": "KNOWS",
                    "from_label": "Person",
                    "from_key": "bob",
                    "to_label": "Person",
                    "to_key": "carol",
                }
            ]
        },
    )
    assert res.status_code == 200
    summary = MutationSummary.model_validate(res.json())
    assert summary.edges == 1


def test_query(seeded_client):
    res = seeded_client.post(
        "/api/query",
        json={"cypher": "MATCH (p:Person) RETURN p.id ORDER BY p.id"},
    )
    assert res.status_code == 200
    q = QueryResponse.model_validate(res.json())
    assert q.columns == ["p.id"]
    assert q.rows == [["alice"], ["bob"]]
    assert q.row_count == 2
    assert q.truncated is False


def test_query_excludes_internal_tables(seeded_client):
    """define_schema wrote _grag_tables registry rows; a bare MATCH (n) spans
    that internal table too, but its rows must not reach the user."""
    res = seeded_client.post("/api/query", json={"cypher": "MATCH (n) RETURN n"})
    assert res.status_code == 200
    q = QueryResponse.model_validate(res.json())
    assert q.row_count == 2
    assert [row[0]["_LABEL"] for row in q.rows] == ["Person", "Person"]
    assert q.subgraph.nodes  # alice + bob made it into the subgraph
    assert all(not n.label.startswith("_") for n in q.subgraph.nodes)
    assert all(not e.type.startswith("_") for e in q.subgraph.edges)

    sample = GraphSample.model_validate(
        seeded_client.get("/api/graph/sample").json()
    )
    assert all(not n.label.startswith("_") for n in sample.subgraph.nodes)
    assert all(not k.startswith("_") for k in sample.stats.labels)


def test_query_rejects_write_with_hint(seeded_client):
    res = seeded_client.post(
        "/api/query",
        json={"cypher": "CREATE (n:Person {id: 'x'})"},
    )
    assert res.status_code == 403
    body = res.json()
    assert "read-only" in body["error"]
    assert body["hint"]


def test_search_returns_seeds_and_context(seeded_client):
    res = seeded_client.post("/api/search", json={"query": "Alice"})
    assert res.status_code == 200
    sr = SearchResponse.model_validate(res.json())
    assert len(sr.seeds) >= 1
    assert any(s.node.id == "Person:alice" for s in sr.seeds)
    assert "alice" in sr.context


def test_context_endpoint(seeded_client):
    search = seeded_client.post("/api/search", json={"query": "Alice"})
    seeds = SearchResponse.model_validate(search.json()).seeds
    node_ids = [s.node.id for s in seeds]
    assert node_ids

    res = seeded_client.post(
        "/api/context", json={"node_ids": node_ids, "hops": 1}
    )
    assert res.status_code == 200
    ctx = ContextResponse.model_validate(res.json())
    assert ctx.context
    assert ctx.token_estimate > 0
    assert "Person:alice" in ctx.included_node_ids
    assert ctx.subgraph.nodes


def test_graph_sample(seeded_client):
    res = seeded_client.get("/api/graph/sample")
    assert res.status_code == 200
    sample = GraphSample.model_validate(res.json())
    assert sample.stats.node_count >= 2
    assert sample.stats.labels.get("Person") == 2
    assert {n.id for n in sample.subgraph.nodes} >= {"Person:alice", "Person:bob"}

    labeled = seeded_client.get("/api/graph/sample", params={"limit": 1, "label": "Person"})
    assert labeled.status_code == 200
    ls = GraphSample.model_validate(labeled.json())
    assert all(n.label == "Person" for n in ls.subgraph.nodes)


def test_ingest(client):
    res = client.post(
        "/api/ingest",
        json={
            "documents": [
                {
                    "text": "LadybugDB is an embedded graph database. "
                    "grag sits on top of it and serves LLM agents.",
                    "source": "readme.md",
                }
            ]
        },
    )
    if res.status_code == 400 and "not implemented" in res.json()["error"]:
        # grag.ingest.loaders lands in a concurrent wave; the endpoint must
        # still surface the facade's "not implemented" as a clean 400.
        pytest.skip("grag.ingest.loaders not implemented yet")
    assert res.status_code == 200
    out = IngestResponse.model_validate(res.json())
    assert out.label == "Chunk"
    assert out.nodes_created >= 1


def test_missing_edge_endpoints_returns_404(seeded_client):
    res = seeded_client.post(
        "/api/edges/upsert",
        json={
            "edges": [
                {
                    "type": "KNOWS",
                    "from_label": "Person",
                    "from_key": "alice",
                    "to_label": "Person",
                    "to_key": "ghost",
                }
            ]
        },
    )
    assert res.status_code == 404
    body = res.json()
    assert "ghost" in body["error"]
    assert body["hint"]


def test_bad_cypher_returns_400(seeded_client):
    res = seeded_client.post("/api/query", json={"cypher": "MATCH (n WHERE"})
    assert res.status_code == 400
    body = res.json()
    assert body["error"]
    assert "hint" in body


def test_root_fallback(client):
    res = client.get("/")
    assert res.status_code == 200
    if _static_built():
        # UI built: SPA is served at / and for unknown non-api GET paths.
        assert "text/html" in res.headers["content-type"]
        deep = client.get("/some/deep/link")
        assert deep.status_code == 200
        assert "text/html" in deep.headers["content-type"]
        # real API misses still 404 as JSON
        missing = client.get("/api/nope")
        assert missing.status_code == 404
    else:
        body = res.json()
        assert body["service"] == "grag"
        assert body["version"] == grag.__version__
