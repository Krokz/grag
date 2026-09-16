"""The SVG download must stay complete past ordinary API/work reply limits."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi.testclient import TestClient

from grag.api.graph_export import capture_graph_export
from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.errors import CypherError, FreshnessError, GragError
from grag.core.limits import MAX_RESPONSE_BYTES
from grag.core.types import FreshnessReport, GraphSample


@pytest.fixture()
def client(tmp_path):
    app = create_app(GragConfig(db_path=tmp_path / "svg.lbdb", buffer_pool_size=128 * 1024**2))
    with TestClient(app) as client:
        yield client


def test_large_export_preserves_complete_topology_without_bodies(client):
    engine = client.app.state.service.engine
    engine.execute_write("CREATE NODE TABLE Item(key STRING PRIMARY KEY, body STRING)")
    engine.execute_write("CREATE NODE TABLE Isolated(number INT64 PRIMARY KEY)")
    engine.execute_write("CREATE NODE TABLE Empty(key STRING PRIMARY KEY)")
    engine.execute_write("CREATE NODE TABLE _Private(key STRING PRIMARY KEY)")
    engine.execute_write("CREATE (:Isolated {number:42})")
    engine.execute_write("CREATE (:_Private {key:'hidden'})")
    engine.execute_write("CREATE REL TABLE NEXT(FROM Item TO Item)")
    prefix = 'source:&<"🧭:' + "x" * 180
    engine.execute_write(
        "UNWIND range(0,5999) AS i CREATE (:Item {key:$prefix + CAST(i AS STRING), body:$body})",
        {"prefix": prefix, "body": "unused source body " * 30},
    )
    # All adjacent edges, one parallel edge and a self-loop must survive.
    engine.execute_write(
        "UNWIND range(0,5998) AS i "
        "MATCH (a:Item),(b:Item) WHERE a.key=$prefix + CAST(i AS STRING) "
        "AND b.key=$prefix + CAST(i+1 AS STRING) CREATE (a)-[:NEXT]->(b)",
        {"prefix": prefix},
    )
    for target in (0, 1):
        engine.execute_write(
            "MATCH (a:Item {key:$source_key}),(b:Item {key:$target_key}) CREATE (a)-[:NEXT]->(b)",
            {"source_key": prefix + "0", "target_key": prefix + str(target)},
        )
    old = client.get("/api/graph/full")
    assert old.status_code == 413
    assert old.json()["resource"] == "response_bytes"

    response = client.get("/api/graph/export")
    assert response.status_code == 200
    assert len(response.content) > MAX_RESPONSE_BYTES
    assert response.headers["content-type"].startswith("application/json")
    graph = GraphSample.model_validate(response.json())
    assert graph.stats.node_count == len(graph.subgraph.nodes) == 6001
    assert graph.stats.edge_count == len(graph.subgraph.edges) == 6001
    assert graph.stats.labels == {"Empty": 0, "Isolated": 1, "Item": 6000, "NEXT": 6001}
    nodes = {n.id: n for n in graph.subgraph.nodes}
    assert len(nodes) == 6001
    assert nodes["Isolated:42"].properties == {"number": 42}
    assert nodes["Item:" + prefix + "0"].properties == {"key": prefix + "0"}
    assert "unused source body" not in response.text
    assert "_Private" not in response.text
    assert len({e.id for e in graph.subgraph.edges}) == 6001
    assert all(e.source in nodes and e.target in nodes and not e.properties for e in graph.subgraph.edges)
    assert sum(e.source == e.target for e in graph.subgraph.edges) == 1
    assert sum(e.source == "Item:" + prefix + "0" and e.target == "Item:" + prefix + "1"
               for e in graph.subgraph.edges) == 2


def test_empty_export_and_freshness_policy(client, monkeypatch):
    service = client.app.state.service
    policies = []

    def read(policy):
        policies.append(policy)
        return FreshnessReport(status="fresh")

    monkeypatch.setattr(service, "read_freshness", read)
    response = client.get("/api/graph/export?freshness=require&freshness_timeout_ms=123")
    assert response.status_code == 200
    graph = GraphSample.model_validate(response.json())
    assert not graph.subgraph.nodes and not graph.subgraph.edges
    assert graph.freshness.status == "fresh"
    assert policies[0].freshness == "require"
    assert policies[0].freshness_timeout_ms == 123


def test_require_fresh_failure_never_starts_download(client, monkeypatch):
    def fail(policy):
        raise FreshnessError("Source not verified", freshness={"status": "stale"}, hint="Retry verification.")

    monkeypatch.setattr(client.app.state.service, "read_freshness", fail)
    response = client.get("/api/graph/export?freshness=require")
    assert response.status_code == 503
    assert response.json()["code"] == "freshness_unavailable"
    assert "content-disposition" not in response.headers


def test_export_requires_configured_token(tmp_path):
    app = create_app(GragConfig(
        db_path=tmp_path / "auth.lbdb", buffer_pool_size=128 * 1024**2,
        api_token="test-export",  # noqa: S106 — synthetic test token
    ))
    with TestClient(app) as client:
        assert client.get("/api/graph/export").status_code == 401
        response = client.get("/api/graph/export", headers={"Authorization": "Bearer test-export"})
        assert response.status_code == 200


def test_grouped_relationships_fail_instead_of_silently_omitting_edges(client):
    engine = client.app.state.service.engine
    engine.execute_write("CREATE NODE TABLE One(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE NODE TABLE Two(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE REL TABLE GROUP Related(FROM One TO One, FROM One TO Two)")
    response = client.get("/api/graph/export")
    assert response.status_code == 400
    assert "exactly one endpoint pair" in response.json()["error"]


def test_capture_is_consistent_and_releases_writer_before_download(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Item(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE (:Item {id:'before'})")
    attempting = Event()
    finished = Event()
    original = engine.iter_rows

    def change():
        attempting.set()
        engine.execute_write("CREATE (:Item {id:'after'})")
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def rows(query):
            nonlocal future
            if query.startswith("MATCH (n:Item)"):
                future = pool.submit(change)
                assert attempting.wait(5)
                assert not finished.wait(0.05)
            yield from original(query)

        monkeypatch.setattr(engine, "iter_rows", rows)
        with capture_graph_export(engine, engine.config, FreshnessReport()) as stream:
            assert future is not None
            future.result(timeout=5)  # client hasn't read the file; writer is free
            graph = GraphSample.model_validate(json.load(stream))
            assert [n.id for n in graph.subgraph.nodes] == ["Item:before"]
            assert graph.stats.node_count == 1
        assert stream.closed


def test_capture_failure_closes_spool_before_http_success(client, monkeypatch):
    import grag.api.graph_export as module

    engine = client.app.state.service.engine
    engine.execute_write("CREATE NODE TABLE Item(id STRING PRIMARY KEY)")
    files = []
    real_tempfile = module.tempfile.TemporaryFile

    @contextmanager
    def tracked(**kwargs):
        with real_tempfile(**kwargs) as file:
            files.append(file)
            yield file

    def fail(query):
        yield {"key": "partial"}
        raise CypherError("export failed midway")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", tracked)
    monkeypatch.setattr(engine, "iter_rows", fail)
    response = client.get("/api/graph/export")
    assert response.status_code >= 400
    assert "export failed midway" in response.text
    assert "content-disposition" not in response.headers
    assert len(files) == 1 and files[0].closed
    assert client.app.state.service.shutdown_status()["active_operations"] == 0


def test_export_refuses_uncommitted_state(engine):
    with (
        engine.write_transaction(),
        pytest.raises(GragError, match="uncommitted"),
        capture_graph_export(engine, engine.config, FreshnessReport()),
    ):
        pytest.fail("Uncommitted graph was exposed")
