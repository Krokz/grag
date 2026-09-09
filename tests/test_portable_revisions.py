"""M31: versioned, entity-scoped edge guards survive logical storage changes."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import TypeAdapter

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConflictError, SchemaError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.revisions import _REVISION_METADATA, annotate_revisions, content_revision
from grag.core.types import (
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    QueryRequest,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.mcp_server import server as mcp
from grag.service import GragService
from grag.transfer import export_lines, import_from


def legacy_revision(value):
    evidence = {k: v for k, v in value.items() if v is not None and (not k.startswith("_") or k in _REVISION_METADATA)}
    encoded = TypeAdapter(dict).dump_python(evidence, mode="json")
    return hashlib.sha256(("grag-content-v1:" + json.dumps(encoded, sort_keys=True, ensure_ascii=False, separators=(",", ":"))).encode()).hexdigest()


@pytest.fixture()
def graph(engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[
        NodeTableSpec(name="Zed", primary_key="slug", properties=[PropertySpec(name="body")]),
        NodeTableSpec(name="Alpha", primary_key="code", properties=[PropertySpec(name="code", type="INT64")]),
    ], rel_tables=[RelTableSpec(name="LINKS", from_label="Zed", to_label="Alpha", properties=[PropertySpec(name="reason")])]))
    upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(label="Zed", key="z"), UpsertNode(label="Alpha", key=42)]))
    return engine


def edge(**kwargs):
    return UpsertEdge(type="LINKS", from_label="Zed", from_key="z", to_label="Alpha", to_key=42,
                      properties={"reason": "evidence"}, source="decision.md", **kwargs)


def raw(engine):
    return engine.execute("MATCH ()-[r:LINKS]->() RETURN r").rows[0][0]


def test_guard_survives_reordered_tables_typed_keys_and_restore(graph):
    original = upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge()], operation_id="created"))
    old = raw(graph)
    token = content_revision(old)
    assert token == original.revisions["LINKS:Zed:z->Alpha:42"]
    with Engine(GragConfig(db_path=":memory:")) as restored:
        import_from(restored, restored.config, export_lines(graph))
        value = raw(restored)
        assert (old["_SRC"], old["_DST"]) != (value["_SRC"], value["_DST"])
        assert content_revision(value) == token
        result = upsert_edges(restored, restored.config, UpsertEdgesRequest(edges=[
            edge(expected_revision=token).model_copy(update={"properties": {"reason": "changed"}})]))
        assert result.revisions["LINKS:Zed:z->Alpha:42"] != token
        with pytest.raises(ConflictError):
            upsert_edges(restored, restored.config, UpsertEdgesRequest(edges=[edge(expected_revision=token)]))


def test_legacy_receipt_and_guard_transition_are_explicit(graph, monkeypatch):
    import grag.core.mutations as mutations
    first = UpsertEdgesRequest(edges=[edge()], operation_id="old-created")
    with monkeypatch.context() as patch:
        patch.setattr(mutations, "content_revision", legacy_revision)
        old = upsert_edges(graph, graph.config, first)
    legacy = old.revisions["LINKS:Zed:z->Alpha:42"]
    # Emulate a saved pre-transition guarded receipt using the exact old wire
    # request; its digest must remain replayable even though its guard is retired.
    retry = UpsertEdgesRequest(edges=[edge(expected_revision=legacy)], operation_id="old-guarded")
    real_prepare = mutations._prepare
    def old_prepare(engine, request):
        unguarded = request.model_copy(update={"edges": [e.model_copy(update={"expected_revision": None}) for e in request.edges]})
        return real_prepare(engine, unguarded)
    with monkeypatch.context() as patch:
        patch.setattr(mutations, "_prepare", old_prepare)
        patch.setattr(mutations, "content_revision", legacy_revision)
        old_guarded = upsert_edges(graph, graph.config, retry)
    for engine in (graph,):
        assert upsert_edges(engine, engine.config, retry).model_dump(exclude={"replayed"}) == old_guarded.model_dump(exclude={"replayed"})
        with pytest.raises(ConflictError, match="Legacy relationship revision") as failure:
            upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(label="Zed", key="z", properties={"body": "must not land"})], edges=[edge(expected_revision=legacy)]))
        assert "r2:" in failure.value.hint
        assert engine.execute("MATCH (n:Zed) RETURN n.body").rows == [[None]]
    with Engine(GragConfig(db_path=":memory:")) as restored:
        import_from(restored, restored.config, export_lines(graph))
        assert upsert_edges(restored, restored.config, retry).replayed
        assert upsert_edges(restored, restored.config, retry).revisions == old_guarded.revisions
        new = content_revision(raw(restored))
        assert new.startswith("r2:")
        upsert_edges(restored, restored.config, UpsertEdgesRequest(edges=[edge(expected_revision=new)]))


def test_relationship_token_scopes_content_not_global_identity(graph):
    upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge()]))
    original = raw(graph)
    token = content_revision(original)
    assert content_revision({**original, "_ID": {"table": 99, "offset": 42}, "_SRC": {}, "_DST": {}}) == token
    assert content_revision({**original, "_source": "different"}) != token
    assert content_revision({**original, "_LABEL": "OTHER"}) != token
    assert content_revision({**original, "reason": "different"}) != token
    node = graph.execute("MATCH (n:Zed) RETURN n").rows[0][0]
    assert content_revision(node) == legacy_revision(node)  # node/history format unchanged


def test_native_nested_edges_all_present_same_token(graph):
    upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge()]))
    value = raw(graph)
    result = annotate_revisions({"map": {"edge": value}, "list": [value], "path": {"_NODES": [], "_RELS": [value]}})
    expected = content_revision(value)
    assert result["map"]["edge"]["_revision"] == result["list"][0]["_revision"] == result["path"]["_RELS"][0]["_revision"] == expected


def test_return_relationship_alone_needs_no_endpoint_lookup(tmp_path, monkeypatch):
    svc = GragService(GragConfig(db_path=tmp_path / "single.lbdb", buffer_pool_size=128 * 1024**2))
    try:
        define_schema(svc.engine, svc.config, DefineSchemaRequest(node_tables=[NodeTableSpec(name="Note")], rel_tables=[RelTableSpec(name="LINKS", from_label="Note", to_label="Note")]))
        svc.upsert_nodes(UpsertNodesRequest(nodes=[UpsertNode(label="Note", key="n")], edges=[UpsertEdge(type="LINKS", from_label="Note", from_key="n", to_label="Note", to_key="n")]))
        monkeypatch.setattr(svc, "_pk_map", lambda: pytest.fail("unneeded catalog/endpoint lookup"))
        reply = svc.cypher_query(QueryRequest(cypher="MATCH ()-[r:LINKS]->() RETURN r"))
        token = reply.rows[0][0]["_revision"]
        assert token.startswith("r2:")
        assert json.loads(mcp.cypher_query(svc, "MATCH ()-[r:LINKS]->() RETURN r"))["rows"][0][0]["_revision"] == token
        assert svc.upsert_edges(UpsertEdgesRequest(edges=[UpsertEdge(type="LINKS", from_label="Note", from_key="n", to_label="Note", to_key="n", expected_revision=token)])).edges == 1
    finally:
        svc.close()


def test_concurrent_portable_guards_have_one_winner_and_duplicates_still_fail(graph):
    upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge()]))
    token = content_revision(raw(graph))
    def update(reason):
        try:
            upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge(expected_revision=token).model_copy(update={"properties": {"reason": reason}})]))
            return True
        except ConflictError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(update, ("one", "two"))) == [False, True]
    graph.execute_write("MATCH (a:Zed), (b:Alpha) CREATE (a)-[:LINKS]->(b)")
    with pytest.raises(SchemaError, match="Multiple relationships"):
        upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[edge(expected_revision=token)]))
