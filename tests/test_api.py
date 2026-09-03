"""End-to-end tests for the REST API (grag.api.main.create_app)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import grag
from grag.api.main import _STATIC_DIR, create_app
from grag.config import GragConfig, database_identity
from grag.core.errors import ConfigurationError
from grag.core.types import (
    CodeIngestResponse,
    ContextResponse,
    DefineSchemaRequest,
    GraphSample,
    IngestResponse,
    MutationSummary,
    QueryResponse,
    SchemaDocument,
    SearchResponse,
    UpsertNodesRequest,
)
from grag.service import GragService


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
    assert res.json() == {
        "status": "ok",
        "version": grag.__version__,
        "database_id": database_identity(client.app.state.service.config.db_path),
        "server_id": database_identity(client.app.state.service.config.db_path),
        "pid": os.getpid(),
        "mcp_enabled": False,
        "mcp_path": None,
        "embedding": None,
        "code_index": {"refreshes": 0, "tracked": 0, "running": False, "last_error": None},
    }


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
        json={
            "nodes": [
                {"label": "Person", "key": "carol", "properties": {"name": "Carol"}}
            ]
        },
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

    sample = GraphSample.model_validate(seeded_client.get("/api/graph/sample").json())
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

    res = seeded_client.post("/api/context", json={"node_ids": node_ids, "hops": 1})
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

    labeled = seeded_client.get(
        "/api/graph/sample", params={"limit": 1, "label": "Person"}
    )
    assert labeled.status_code == 200
    ls = GraphSample.model_validate(labeled.json())
    assert all(n.label == "Person" for n in ls.subgraph.nodes)

    # A labeled sample scopes edges to that label's 1-hop neighborhood (so a
    # label view shows the label *and its relationships*, not the whole graph's
    # edges). With alice -[KNOWS]-> bob present, the KNOWS edge appears.
    labeled2 = seeded_client.get("/api/graph/sample", params={"label": "Person"})
    ls2 = GraphSample.model_validate(labeled2.json())
    assert any(e.type == "KNOWS" for e in ls2.subgraph.edges)


def test_graph_full_returns_every_node_and_edge(seeded_client):
    # Sample is a clamped window; full is the whole database, so a limit=1
    # sample must be a strict subset of it and full must carry every edge.
    extra = seeded_client.post(
        "/api/nodes/upsert",
        json={
            "nodes": [
                {"label": "Person", "key": f"p{i}", "properties": {"name": f"P{i}"}}
                for i in range(5)
            ]
        },
    )
    assert extra.status_code == 200

    res = seeded_client.get("/api/graph/full")
    assert res.status_code == 200
    full = GraphSample.model_validate(res.json())
    assert len(full.subgraph.nodes) == full.stats.node_count == 7
    assert len(full.subgraph.edges) == full.stats.edge_count == 1
    assert {n.id for n in full.subgraph.nodes} >= {"Person:alice", "Person:bob"}
    assert any(e.type == "KNOWS" for e in full.subgraph.edges)
    assert not any(n.label.startswith("_") for n in full.subgraph.nodes)

    window = seeded_client.get("/api/graph/sample", params={"limit": 1}).json()
    assert len(window["subgraph"]["nodes"]) < len(full.subgraph.nodes)


def test_graph_sample_rejects_injected_label_without_mutating(seeded_client):
    payload = "Person) RETURN n; MATCH (x:Person) DELETE x; //"
    res = seeded_client.get("/api/graph/sample", params={"label": payload})
    assert res.status_code == 400
    assert "Invalid node label" in res.json()["error"]

    count = seeded_client.post(
        "/api/query", json={"cypher": "MATCH (n:Person) RETURN count(n)"}
    )
    assert count.status_code == 200
    assert count.json()["rows"] == [[2]]


def test_graph_sample_rejects_unknown_label(seeded_client):
    res = seeded_client.get("/api/graph/sample", params={"label": "Missing"})
    assert res.status_code == 400
    assert "Unknown node label" in res.json()["error"]


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


def test_ingest_code(client, tmp_path):
    from grag.ingest.code import _repo_id

    pkg = tmp_path / "apipkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def f(x: int) -> int:\n    return x\n", encoding="utf-8"
    )
    res = client.post("/api/ingest/code", json={"paths": [str(pkg)]})
    assert res.status_code == 200
    out = CodeIngestResponse.model_validate(res.json())
    assert (out.repos, out.modules, out.classes, out.functions) == (1, 1, 0, 1)
    assert out.edges == 2  # CONTAINS_REPO_MODULE + CONTAINS_MODULE_FUNCTION
    assert out.warnings == []

    q = client.post("/api/query", json={"cypher": "MATCH (f:Function) RETURN f.id"})
    assert q.status_code == 200
    assert QueryResponse.model_validate(q.json()).rows == [
        [f"{_repo_id(pkg)}:mod.py#f"]
    ]

    # multi-db selectors are honored like on every other route (single-db: ignored)
    again = client.post("/api/ingest/code?db=anything", json={"paths": [str(pkg)]})
    assert again.status_code == 200


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


# -- transport security ---------------------------------------------------------


def test_version_matches_package_metadata():
    """Three-way guard: __version__ == installed dist metadata == pyproject.
    The pyproject leg also catches stale editable installs (metadata frozen at
    install time) — reinstall with `pip install -e .` if this fails."""
    import re
    from importlib.metadata import version as dist_version

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert m, "pyproject.toml is missing a version field"
    assert grag.__version__ == dist_version("gragdb") == m.group(1)


def test_cors_never_wildcard(client):
    """The API serves its UI same-origin, so no cross-origin allowance exists
    by default — a drive-by page must get no CORS grant (and never '*')."""
    preflight = client.options(
        "/api/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") is None

    res = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200  # server answers; the browser blocks the read
    assert res.headers.get("access-control-allow-origin") is None


def test_cors_configured_origin_allowed(tmp_path):
    app = create_app(
        GragConfig(
            db_path=tmp_path / "cors.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            cors_origins=["https://ui.example"],
        )
    )
    with TestClient(app) as c:
        res = c.get("/api/health", headers={"Origin": "https://ui.example"})
        assert res.headers.get("access-control-allow-origin") == "https://ui.example"
        evil = c.get("/api/health", headers={"Origin": "https://evil.example"})
        assert evil.headers.get("access-control-allow-origin") is None


def test_dns_rebinding_host_header_rejected(client):
    """A rebinding attack arrives with the attacker's Host header; the
    TrustedHost allow-list (loopbacks only by default) rejects it."""
    res = client.get("/api/health", headers={"host": "evil.example"})
    assert res.status_code == 400


@pytest.mark.parametrize(
    "mcp_path",
    [
        "/",
        "/api",
        "/api/mcp",
        "/api/admin/stop",
        "/assets",
        "/assets/index.js",
        "//evil.example/mcp",
        "/mcp?target=evil",
        "/mcp#fragment",
        "/mcp\\child",
        "/mcp child",
        "/mcp\x00child",
        "/mcp\ud800",
    ],
)
def test_mounted_mcp_path_cannot_shadow_app_routes(mcp_path, tmp_path):
    with pytest.raises(ConfigurationError, match="cannot overlap"):
        create_app(
            GragConfig(
                db_path=tmp_path / "invalid-mcp-path.lbdb",
                buffer_pool_size=128 * 1024 * 1024,
                mcp_path=mcp_path,
            )
        )


@pytest.mark.parametrize("mcp_path", ["/agent-mcp", "/assets2"])
def test_safe_custom_mcp_mount_starts_and_is_reported(mcp_path, tmp_path):
    app = create_app(
        GragConfig(
            db_path=tmp_path / "custom-mcp-path.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            mcp_path=mcp_path,
        )
    )

    assert any(
        getattr(route, "path", None) == mcp_path
        and getattr(route, "name", None) == "mcp"
        for route in app.routes
    )
    with TestClient(app) as custom_client:
        health = custom_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mcp_enabled"] is True
        assert health.json()["mcp_path"] == mcp_path


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "", "192.0.2.10", "grag.example"],  # noqa: S104
)
@pytest.mark.parametrize("mcp_path", [None, "/mcp"])
def test_non_loopback_bind_requires_token(host, mcp_path, tmp_path):
    """The startup gate covers REST/UI and the optional mounted MCP app."""

    with pytest.raises(ConfigurationError, match="GRAG_API_TOKEN"):
        create_app(
            GragConfig(
                db_path=tmp_path / "unsafe.lbdb",
                buffer_pool_size=128 * 1024 * 1024,
                host=host,
                mcp_path=mcp_path,
            )
        )


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1", "[::1]"]
)
def test_loopback_bind_does_not_require_token(host, tmp_path):
    app = create_app(
        GragConfig(
            db_path=tmp_path / "loopback.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            host=host,
        )
    )
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_token_allows_non_loopback_bind_with_mounted_mcp(tmp_path):
    app = create_app(
        GragConfig(
            db_path=tmp_path / "authenticated.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            host="0.0.0.0",  # noqa: S104 — config under test, no socket bind
            api_token="sekret",  # noqa: S106 — test fixture
            mcp_path="/mcp",
        )
    )
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/schema").status_code == 401
        assert c.get("/mcp/").status_code == 401


@pytest.fixture()
def token_client(tmp_path):
    app = create_app(
        GragConfig(
            db_path=tmp_path / "tok.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            api_token="sekret",  # noqa: S106 — test fixture, not a real secret
        )
    )
    with TestClient(app) as c:
        yield c


def test_bearer_token_required_when_configured(token_client):
    assert token_client.get("/api/schema").status_code == 401
    assert (
        token_client.post("/api/query", json={"cypher": "RETURN 1"}).status_code == 401
    )
    ok = token_client.get("/api/schema", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
    wrong = token_client.get("/api/schema", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


def test_health_exempt_from_token(token_client):
    assert token_client.get("/api/health").status_code == 200


def test_managed_shutdown_route_unavailable_without_both_state_values(client):
    assert client.post("/api/admin/stop").status_code == 404

    client.app.state.shutdown_token = "stop-secret"  # noqa: S105 — test token
    assert (
        client.post(
            "/api/admin/stop", headers={"x-grag-stop-token": "stop-secret"}
        ).status_code
        == 404
    )


def test_managed_shutdown_requires_own_exact_token_and_invokes_callback(token_client):
    calls = []
    token_client.app.state.shutdown_token = "stop-secret"  # noqa: S105 — test token
    token_client.app.state.request_shutdown = lambda: calls.append("stop")

    # This private route is exempt from the ordinary bearer middleware, but
    # the separate shutdown token is mandatory and compared exactly.
    assert token_client.post("/api/admin/stop").status_code == 401
    assert (
        token_client.post(
            "/api/admin/stop", headers={"x-grag-stop-token": "wrong"}
        ).status_code
        == 401
    )
    response = token_client.post(
        "/api/admin/stop", headers={"x-grag-stop-token": "stop-secret"}
    )
    assert response.status_code == 202
    assert response.json() == {"status": "stopping"}
    assert calls == ["stop"]


def test_500_returns_generic_body(tmp_path, monkeypatch):
    """Unhandled exceptions must not leak str(exc) (paths, driver internals)."""

    def boom(self, req):
        raise RuntimeError("/secret/path exploded")

    monkeypatch.setattr(GragService, "cypher_query", boom)
    app = create_app(
        GragConfig(db_path=tmp_path / "err.lbdb", buffer_pool_size=128 * 1024 * 1024)
    )
    # raise_server_exceptions is baked into the transport at construction.
    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.post("/api/query", json={"cypher": "RETURN 1"})
    assert res.status_code == 500
    assert res.json() == {"error": "Internal server error.", "hint": None}
    assert "/secret/path" not in res.text


# -- multi-db routing ----------------------------------------------------------


def test_list_dbs_single_db_mode(client):
    res = client.get("/api/dbs")
    assert res.status_code == 200
    assert res.json() == {"dbs": [], "default": None}


def test_single_db_mode_ignores_db_selector(client):
    """A ?db= / x-grag-db selector in single-db mode is ignored, not an error."""
    res = client.post(
        "/api/schema/define?db=anything",
        json={
            "node_tables": [
                {
                    "name": "Doc",
                    "primary_key": "id",
                    "properties": [{"name": "id", "type": "STRING"}],
                }
            ]
        },
    )
    assert res.status_code == 200
    got = client.get("/api/schema", headers={"x-grag-db": "anything"})
    assert got.status_code == 200
    doc = SchemaDocument.model_validate(got.json())
    assert any(t.name == "Doc" for t in doc.node_tables)


def _make_db(path: Path, row_key: str, row_name: str) -> None:
    svc = GragService(GragConfig(db_path=path, buffer_pool_size=128 * 1024 * 1024))
    try:
        svc.define_schema(
            DefineSchemaRequest(
                node_tables=[
                    {
                        "name": "Item",
                        "primary_key": "id",
                        "properties": [
                            {"name": "id", "type": "STRING"},
                            {"name": "name", "type": "STRING"},
                        ],
                    }
                ]
            )
        )
        svc.upsert_nodes(
            UpsertNodesRequest(
                nodes=[
                    {
                        "label": "Item",
                        "key": row_key,
                        "properties": {"name": row_name},
                    }
                ]
            )
        )
    finally:
        svc.close()


@pytest.fixture()
def multi_client(tmp_path):
    """Two dbs (alpha, beta) in a db_dir; alpha is the preferred default."""
    db_dir = tmp_path / "dbs"
    db_dir.mkdir()
    _make_db(db_dir / "alpha.lbdb", "a1", "alpha-thing")
    _make_db(db_dir / "beta.lbdb", "b1", "beta-thing")
    app = create_app(
        GragConfig(
            db_dir=db_dir,
            db_path=Path("alpha.lbdb"),
            buffer_pool_size=128 * 1024 * 1024,
        )
    )
    with TestClient(app) as c:
        yield c


def _item_names(res) -> list[str]:
    assert res.status_code == 200
    q = QueryResponse.model_validate(res.json())
    return [row[0] for row in q.rows]


def test_list_dbs_multi_db_mode(multi_client):
    res = multi_client.get("/api/dbs")
    assert res.status_code == 200
    assert res.json() == {"dbs": ["alpha", "beta"], "default": "alpha"}


def test_multi_db_health_identifies_server_directory(multi_client):
    body = multi_client.get("/api/health").json()
    db_dir = multi_client.app.state.registry.config.db_dir
    assert db_dir is not None
    assert body["server_id"] == database_identity(db_dir)
    assert body["pid"] == os.getpid()
    assert body["mcp_enabled"] is False
    assert body["mcp_path"] is None


def test_query_routes_per_db(multi_client):
    cypher = "MATCH (i:Item) RETURN i.name"
    alpha = multi_client.post("/api/query?db=alpha", json={"cypher": cypher})
    beta = multi_client.post("/api/query?db=beta", json={"cypher": cypher})
    assert _item_names(alpha) == ["alpha-thing"]
    assert _item_names(beta) == ["beta-thing"]


def test_header_routes_like_query_param(multi_client):
    cypher = "MATCH (i:Item) RETURN i.name"
    res = multi_client.post(
        "/api/query", json={"cypher": cypher}, headers={"x-grag-db": "beta"}
    )
    assert _item_names(res) == ["beta-thing"]


def test_query_param_wins_over_header(multi_client):
    cypher = "MATCH (i:Item) RETURN i.name"
    res = multi_client.post(
        "/api/query?db=alpha", json={"cypher": cypher}, headers={"x-grag-db": "beta"}
    )
    assert _item_names(res) == ["alpha-thing"]


def test_unknown_db_returns_404_with_hint(multi_client):
    res = multi_client.post(
        "/api/query?db=nope", json={"cypher": "MATCH (i:Item) RETURN i.name"}
    )
    assert res.status_code == 404
    body = res.json()
    assert "nope" in body["error"]
    assert "alpha" in body["hint"]
    assert "beta" in body["hint"]


def test_default_db_resolves_without_selector(multi_client):
    cypher = "MATCH (i:Item) RETURN i.name"
    res = multi_client.post("/api/query", json={"cypher": cypher})
    assert _item_names(res) == ["alpha-thing"]

    sample = GraphSample.model_validate(multi_client.get("/api/graph/sample").json())
    assert {n.id for n in sample.subgraph.nodes} == {"Item:a1"}


@pytest.fixture()
def no_default_client(tmp_path):
    """Two dbs but db_path names NEITHER, so no default is determinable."""
    db_dir = tmp_path / "dbs"
    db_dir.mkdir()
    _make_db(db_dir / "alpha.lbdb", "a1", "alpha-thing")
    _make_db(db_dir / "beta.lbdb", "b1", "beta-thing")
    app = create_app(
        GragConfig(
            db_dir=db_dir,
            db_path=Path("knowledge.lbdb"),  # not present -> no preferred default
            buffer_pool_size=128 * 1024 * 1024,
        )
    )
    with TestClient(app) as c:
        yield c


def test_server_starts_and_dbs_listable_without_default(no_default_client):
    """Regression: with 2+ DBs and no preferred default, registry.get() raises,
    which used to crash create_app at startup (the server never came up, taking
    /api/dbs discovery down with it). The app must start and serve discovery;
    only selector-less data requests should 400 with the available-db hint."""
    res = no_default_client.get("/api/dbs")
    assert res.status_code == 200
    assert res.json() == {"dbs": ["alpha", "beta"], "default": None}


def test_selectorless_request_400s_but_selected_request_works(no_default_client):
    cypher = "MATCH (i:Item) RETURN i.name"
    # No selector and no default -> 400 with the available-dbs hint.
    res = no_default_client.post("/api/query", json={"cypher": cypher})
    assert res.status_code == 400
    assert "alpha" in res.json()["hint"]
    # Explicit selector still works against the same app.
    ok = no_default_client.post("/api/query?db=beta", json={"cypher": cypher})
    assert _item_names(ok) == ["beta-thing"]


# --- single-process MCP mount (--with-mcp) -----------------------------------------


def _sse_json(text: str):
    """Parse the JSON payload from the last `data:` line of an SSE stream."""
    import json as _json

    payload = None
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
    assert payload, f"no SSE data in reply: {text[:200]}"
    return _json.loads(payload)


def test_serve_with_mcp_mount_shares_one_db(tmp_path):
    """--with-mcp mounts the MCP streamable-http endpoint on the REST app so UI
    + REST + MCP run in one process against the same live .lbdb (single write
    conn). Drives a REAL uvicorn server (the MCP endpoint's DNS-rebinding guard
    is host-based and rejects TestClient's `testserver` host). Verifies: MCP
    handshake + a tool call over /mcp see the SAME data written via REST."""
    import socket
    import threading
    import time
    import urllib.request

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_app(
        GragConfig(
            db_path=tmp_path / "one.lbdb",
            buffer_pool_size=128 * 1024 * 1024,
            mcp_path="/mcp",
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"

    base = f"http://127.0.0.1:{port}"

    def post(path, body, accept="application/json"):
        req = urllib.request.Request(
            base + path,
            data=__import__("json").dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": accept},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode()

    def mcp(method, params, req_id):
        return _sse_json(
            post(
                "/mcp/",
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                accept="application/json, text/event-stream",
            )
        )

    try:
        init = mcp(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
            1,
        )
        assert init["result"]["serverInfo"]["name"] == "grag"

        # Write via the REST surface.
        post(
            "/api/schema/define",
            {
                "node_tables": [
                    {"name": "Note", "primary_key": "id", "properties": []}
                ],
                "rel_tables": [],
            },
        )
        post(
            "/api/nodes/upsert",
            {"nodes": [{"label": "Note", "key": "n1", "properties": {}}]},
        )

        # The MCP surface reads the SAME write (shared registry/service).
        call = mcp(
            "tools/call",
            {
                "name": "cypher_query",
                "arguments": {"cypher": "MATCH (n:Note) RETURN count(n)"},
            },
            2,
        )
        text = call["result"]["content"][0]["text"]
        # LadybugDB rewrites count(n) -> COUNT(n._ID); assert on the count value.
        assert '"rows":[[1]]' in text, text

        # REST health on the same process.
        with urllib.request.urlopen(base + "/api/health", timeout=10) as r:
            assert r.status == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --- online export (backup of a live server) ---------------------------------------


def test_export_endpoint_streams_jsonl(client):
    client.post(
        "/api/schema/define",
        json={
            "node_tables": [{"name": "Thing", "properties": [{"name": "title"}]}],
            "rel_tables": [],
        },
    )
    client.post(
        "/api/nodes/upsert",
        json={"nodes": [{"label": "Thing", "key": "t1", "properties": {"title": "x"}}]},
    )
    res = client.get("/api/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in res.text.splitlines() if line]
    assert lines[0]["type"] == "grag_export"
    assert lines[1]["type"] == "schema"
    assert any(r["type"] == "node" and r["key"] == "t1" for r in lines)


def test_export_endpoint_requires_token(token_client):
    assert token_client.get("/api/export").status_code == 401


def test_export_from_server_streams_to_file(tmp_path, monkeypatch):
    import io

    from grag.transfer import export_from_server

    seen = {}

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["db"] = req.get_header("X-grag-db")
        return Resp(b'{"type":"grag_export"}\n{"type":"schema"}\n')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "backup.jsonl"
    n = export_from_server(
        "https://grag.example.com/",
        str(out),
        api_token="secret",  # noqa: S106 — test fixture
        db_name="algo4",
    )
    assert n == 2
    assert seen == {
        "url": "https://grag.example.com/api/export",
        "auth": "Bearer secret",
        "db": "algo4",
    }
    assert out.read_text().count("\n") == 2
