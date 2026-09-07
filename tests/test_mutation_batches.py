"""M10: atomic memory writes, durable retry receipts, and state preconditions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.engine import Engine, node_record_from_value
from grag.core.errors import ConflictError, CypherError, GragError, SchemaError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.mutations import OPERATIONS_TABLE
from grag.core.revisions import annotate_revisions, content_revision
from grag.core.types import (
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.mcp_server import server as mcp
from grag.service import GragService


@pytest.fixture
def graph(engine):
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(
                    name="Memory",
                    primary_key="id",
                    properties=[
                        PropertySpec(name="body"),
                        PropertySpec(name="rank", type="INT64"),
                        PropertySpec(name="day", type="DATE"),
                    ],
                )
            ],
            rel_tables=[
                RelTableSpec(
                    name="SUPPORTS",
                    from_label="Memory",
                    to_label="Memory",
                    properties=[PropertySpec(name="reason")],
                )
            ],
        ),
    )
    return engine


def node(key="a", body="first", **kwargs):
    return UpsertNode(
        label="Memory", key=key, properties={"body": body}, source="session", **kwargs
    )


def edge(**kwargs):
    return UpsertEdge(
        type="SUPPORTS",
        from_label="Memory",
        from_key="a",
        to_label="Memory",
        to_key="b",
        properties={"reason": "evidence"},
        source="session",
        **kwargs,
    )


def batch(operation_id=None):
    return UpsertNodesRequest(
        nodes=[node(), node("b")], edges=[edge()], operation_id=operation_id
    )


def revision(engine, key="a"):
    return content_revision(
        engine.execute("MATCH (n:Memory {id: $key}) RETURN n", {"key": key}).rows[0][0]
    )


def body(engine, key="a"):
    return engine.execute(
        "MATCH (n:Memory {id: $key}) RETURN n.body", {"key": key}
    ).rows


def test_combined_memory_and_links_commit(graph):
    summary = upsert_nodes(graph, graph.config, batch())
    assert (summary.nodes, summary.edges, summary.warnings) == (2, 1, [])
    assert graph.execute("MATCH (a)-[:SUPPORTS]->(b) RETURN a.id,b.id").rows == [
        ["a", "b"]
    ]


@pytest.mark.parametrize(
    "bad",
    [
        UpsertNode(label="Memory", key="bad", properties={"id": "changed"}),
        UpsertNode(label="Missing", key="bad"),
        UpsertNode(label="Memory", key=None),
        UpsertNode(label="Memory", key=["bad"]),
        UpsertNode(label="Memory", key=42),
    ],
)
def test_entire_batch_validated_before_any_merge(graph, monkeypatch, bad):
    writes = []
    real = graph.execute_write

    def capture(query, params=None):
        if "MERGE" in query or query.startswith("CREATE"):
            writes.append(query)
        return real(query, params)

    monkeypatch.setattr(graph, "execute_write", capture)
    with pytest.raises(GragError):
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(nodes=[node(), bad], operation_id="invalid"),
        )
    assert writes == []
    assert body(graph) == []


@pytest.mark.parametrize(
    "bad",
    [
        edge().model_copy(update={"type": "Missing"}),
        edge().model_copy(update={"to_key": "missing"}),
        edge().model_copy(update={"to_label": "Wrong"}),
        edge().model_copy(update={"from_key": None}),
    ],
)
def test_bad_edge_does_not_save_nodes(graph, bad):
    with pytest.raises(GragError):
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(nodes=[node(), node("b")], edges=[bad]),
        )
    assert body(graph) == []


@pytest.mark.parametrize("stage", ["first_node", "second_node", "edge", "receipt"])
def test_late_failure_rolls_back_data_receipt_and_retry(graph, monkeypatch, stage):
    real = graph.execute_write

    def fail(query, params=None):
        if (
            (
                stage == "first_node"
                and query.startswith("MERGE (n")
                and params["key"] == "a"
            )
            or (
                stage == "second_node"
                and query.startswith("MERGE (n")
                and params["key"] == "b"
            )
            or (stage == "edge" and "MERGE (a)-[r:" in query)
            or (
                stage == "receipt" and query.startswith(f"CREATE (o:{OPERATIONS_TABLE}")
            )
        ):
            raise RuntimeError(f"injected {stage}")
        return real(query, params)

    monkeypatch.setattr(graph, "execute_write", fail)
    with pytest.raises(RuntimeError, match="injected"):
        upsert_nodes(graph, graph.config, batch("retry"))
    assert body(graph) == []
    assert graph.execute("MATCH ()-[r:SUPPORTS]->() RETURN count(r)").rows == [[0]]
    monkeypatch.setattr(graph, "execute_write", real)
    result = upsert_nodes(graph, graph.config, batch("retry"))
    assert not result.replayed and result.nodes == 2 and result.edges == 1
    assert upsert_nodes(graph, graph.config, batch("retry")).replayed


def test_native_statement_failure_rolls_back_prior_node(graph, monkeypatch):
    real = graph.execute_write

    def fail(query, params=None):
        if query.startswith("MERGE (n") and params["key"] == "b":
            return real("MATCH (n:Nonexistent) RETURN n")
        return real(query, params)

    monkeypatch.setattr(graph, "execute_write", fail)
    with pytest.raises(CypherError):
        upsert_nodes(graph, graph.config, batch())
    assert body(graph) == []


def test_edge_only_batch_is_atomic(graph, monkeypatch):
    upsert_nodes(
        graph, graph.config, UpsertNodesRequest(nodes=[node(), node("b"), node("c")])
    )
    real = graph.execute_write

    def fail(query, params=None):
        if "MERGE (a)-[r:" in query and params["tk"] == "c":
            raise RuntimeError("later edge")
        return real(query, params)

    monkeypatch.setattr(graph, "execute_write", fail)
    with pytest.raises(RuntimeError):
        upsert_edges(
            graph,
            graph.config,
            UpsertEdgesRequest(
                edges=[edge(), edge().model_copy(update={"to_key": "c"})]
            ),
        )
    assert graph.execute("MATCH ()-[r:SUPPORTS]->() RETURN count(r)").rows == [[0]]


def test_caught_nested_failure_poison_outer_transaction(graph):
    with (
        pytest.raises(CypherError, match="transaction has failed"),
        graph.write_transaction(),
    ):
        upsert_nodes(graph, graph.config, UpsertNodesRequest(nodes=[node()]))
        with pytest.raises(SchemaError):
            upsert_nodes(
                graph,
                graph.config,
                UpsertNodesRequest(
                    nodes=[
                        node("b"),
                        UpsertNode(label="Memory", key="c", properties={"id": "x"}),
                    ]
                ),
            )
    assert body(graph) == []


def test_retry_is_durable_and_does_not_overwrite_later_edit(graph):
    req = batch("saved")
    first = upsert_nodes(graph, graph.config, req)
    upsert_nodes(graph, graph.config, UpsertNodesRequest(nodes=[node(body="later")]))
    config = graph.config
    graph.close()
    with Engine(config) as reopened:
        replay = upsert_nodes(reopened, config, req)
        assert replay.replayed and replay.revisions == first.revisions
        assert body(reopened) == [["later"]]
        assert replay.revisions["Memory:a"] != revision(reopened)


def test_same_operation_id_different_payload_conflicts(graph):
    upsert_nodes(graph, graph.config, batch("once"))
    with pytest.raises(ConflictError) as error:
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(nodes=[node(body="different")], operation_id="once"),
        )
    assert error.value.code == "operation_id_conflict"
    assert body(graph) == [["first"]]


def test_operation_id_cannot_be_reused_between_tool_kinds(graph):
    upsert_nodes(graph, graph.config, batch("once"))
    with pytest.raises(ConflictError):
        upsert_edges(
            graph, graph.config, UpsertEdgesRequest(edges=[edge()], operation_id="once")
        )


def test_receipt_replay_precedes_schema_and_revision_checks(graph):
    req = UpsertNodesRequest(
        nodes=[node(expected_revision="absent")], operation_id="create"
    )
    upsert_nodes(graph, graph.config, req)
    graph.execute_write("MATCH (n:Memory) DETACH DELETE n")
    assert upsert_nodes(graph, graph.config, req).replayed
    assert body(graph) == []


def test_dict_order_does_not_change_retry_digest(graph):
    a = node().model_copy(update={"properties": {"body": "first", "rank": 1}})
    b = a.model_copy(update={"properties": {"rank": 1, "body": "first"}})
    upsert_nodes(
        graph, graph.config, UpsertNodesRequest(nodes=[a], operation_id="order")
    )
    assert upsert_nodes(
        graph, graph.config, UpsertNodesRequest(nodes=[b], operation_id="order")
    ).replayed


def test_concurrent_same_retry_commits_once(graph):
    def save(_):
        return upsert_nodes(graph, graph.config, batch("parallel"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(8)))
    assert sum(not r.replayed for r in results) == 1
    assert len({r.revisions["Memory:a"] for r in results}) == 1
    assert graph.execute(f"MATCH (o:{OPERATIONS_TABLE}) RETURN count(o)").rows == [[1]]


def test_node_compare_and_swap_and_create_only(graph):
    upsert_nodes(
        graph,
        graph.config,
        UpsertNodesRequest(nodes=[node(expected_revision="absent")]),
    )
    old = revision(graph)
    result = upsert_nodes(
        graph,
        graph.config,
        UpsertNodesRequest(nodes=[node(body="updated", expected_revision=old)]),
    )
    assert result.revisions["Memory:a"] == revision(graph) != old
    for token in (old, "absent"):
        with pytest.raises(ConflictError, match="Revision conflict"):
            upsert_nodes(
                graph,
                graph.config,
                UpsertNodesRequest(nodes=[node(body="stale", expected_revision=token)]),
            )
    assert body(graph) == [["updated"]]


def test_edge_revision_prevents_overwriting_intervening_change(graph):
    first = upsert_nodes(graph, graph.config, batch("edges"))
    token = first.revisions["SUPPORTS:Memory:a->Memory:b"]
    changed = edge(expected_revision=token).model_copy(
        update={"properties": {"reason": "better evidence"}}
    )
    result = upsert_edges(graph, graph.config, UpsertEdgesRequest(edges=[changed]))
    assert result.revisions["SUPPORTS:Memory:a->Memory:b"] != token
    with pytest.raises(ConflictError):
        upsert_edges(
            graph,
            graph.config,
            UpsertEdgesRequest(edges=[edge(expected_revision=token)]),
        )


def test_one_conflict_rolls_back_other_updates(graph):
    upsert_nodes(graph, graph.config, batch())
    with pytest.raises(ConflictError):
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(
                nodes=[
                    node(body="must not land"),
                    node("b", expected_revision="absent"),
                ]
            ),
        )
    assert body(graph) == [["first"]]


def test_two_competing_expected_revisions_only_one_wins(graph):
    upsert_nodes(graph, graph.config, batch())
    token = revision(graph)

    def update(value):
        try:
            upsert_nodes(
                graph,
                graph.config,
                UpsertNodesRequest(nodes=[node(body=value, expected_revision=token)]),
            )
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["left", "right"])) == [False, True]


def test_content_revision_tracks_provenance_but_ignores_vectors_and_generations(graph):
    upsert_nodes(graph, graph.config, batch())
    value = graph.execute("MATCH (n:Memory {id: 'a'}) RETURN n").rows[0][0]
    token = content_revision(value)
    assert (
        content_revision(
            {
                **value,
                "embedding": [1, 2],
                "_emb_fingerprint": "next",
                "_index_generation": "next",
            }
        )
        == token
    )
    assert content_revision({**value, "_source": "changed"}) != token
    assert (
        node_record_from_value(annotate_revisions(value), {"Memory": "id"}).properties[
            "_revision"
        ]
        == token
    )
    # Revisions are content tokens, not monotonic history counters.
    upsert_nodes(graph, graph.config, UpsertNodesRequest(nodes=[node(body="changed")]))
    upsert_nodes(graph, graph.config, UpsertNodesRequest(nodes=[node()]))
    assert revision(graph) == token


def test_virtual_revision_and_private_receipts_cannot_be_written(graph):
    first = upsert_nodes(graph, graph.config, batch("private"))
    result = upsert_nodes(
        graph,
        graph.config,
        UpsertNodesRequest(
            nodes=[node().model_copy(update={"properties": {"_revision": "forged"}})]
        ),
    )
    assert len(result.warnings) == 1
    assert revision(graph) == first.revisions["Memory:a"]
    with pytest.raises(SchemaError):
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(
                nodes=[
                    UpsertNode(
                        label=OPERATIONS_TABLE,
                        key="private",
                        properties={"digest": "forged"},
                    )
                ]
            ),
        )
    with pytest.raises(SchemaError):
        define_schema(
            graph,
            graph.config,
            DefineSchemaRequest(node_tables=[NodeTableSpec(name="_private")]),
        )


@pytest.mark.parametrize(
    "props",
    [
        {"body": 1},
        {"rank": "bad"},
        {"rank": 2**64},
        {"day": "invalid-date"},
        {"_internal": "bad"},
        {"unknown": "bad"},
    ],
)
def test_legacy_skipped_property_warnings_are_preserved(graph, props):
    response = upsert_nodes(
        graph,
        graph.config,
        UpsertNodesRequest(nodes=[node().model_copy(update={"properties": props})]),
    )
    assert response.nodes == 1 and len(response.warnings) == 1


def test_null_can_clear_a_property_with_revision_check(graph):
    upsert_nodes(graph, graph.config, batch())
    result = upsert_nodes(
        graph,
        graph.config,
        UpsertNodesRequest(nodes=[node(body=None, expected_revision=revision(graph))]),
    )
    assert body(graph) == [[None]] and result.warnings == []


def test_date_and_integer_primary_key_normalization(engine):
    engine.execute_write("CREATE NODE TABLE Item(id INT64 PRIMARY KEY, value DATE)")
    request = UpsertNodesRequest(
        nodes=[UpsertNode(label="Item", key="12", properties={"value": "2026-09-06"})],
        operation_id="typed",
    )
    result = upsert_nodes(engine, engine.config, request)
    assert list(result.revisions) == ["Item:12"]
    assert (
        str(engine.execute("MATCH (n:Item) RETURN n.value").rows[0][0]) == "2026-09-06"
    )


def test_mcp_combined_retries_and_readable_conflicts(tmp_path):
    svc = GragService(
        GragConfig(db_path=tmp_path / "mcp.lbdb", buffer_pool_size=128 * 1024**2)
    )
    try:
        svc.engine.execute_write(
            "CREATE NODE TABLE Memory(id STRING PRIMARY KEY, body STRING)"
        )
        svc.engine.execute_write(
            "CREATE REL TABLE SUPPORTS(FROM Memory TO Memory, reason STRING)"
        )
        nodes = [node().model_dump(), node("b").model_dump()]
        first = json.loads(
            mcp.upsert_nodes(svc, nodes, [edge().model_dump()], "mcp-once")
        )
        assert first["edges"] == 1 and not first["replayed"]
        assert json.loads(
            mcp.upsert_nodes(svc, nodes, [edge().model_dump()], "mcp-once")
        )["replayed"]
        response = json.loads(
            mcp.cypher_query(svc, "MATCH (n:Memory {id: 'a'}) RETURN n")
        )
        assert response["rows"][0][0]["_revision"] == first["revisions"]["Memory:a"]
        assert "CODE: revision_conflict" in mcp.upsert_nodes(
            svc, [node(expected_revision="absent").model_dump()]
        )
        assert "CODE: operation_id_conflict" in mcp.upsert_nodes(
            svc, [], [], "mcp-once"
        )
    finally:
        svc.close()


def test_rest_combined_retries_conflict_codes_and_atomic_error(tmp_path):
    app = create_app(
        GragConfig(db_path=tmp_path / "api.lbdb", buffer_pool_size=128 * 1024**2)
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/schema/define",
                json={
                    "node_tables": [
                        {"name": "Memory", "properties": [{"name": "body"}]}
                    ],
                    "rel_tables": [
                        {
                            "name": "SUPPORTS",
                            "from_label": "Memory",
                            "to_label": "Memory",
                            "properties": [{"name": "reason"}],
                        }
                    ],
                },
            ).status_code
            == 200
        )
        request = batch("rest").model_dump(mode="json")
        result = client.post("/api/nodes/upsert", json=request)
        assert result.status_code == 200 and result.json()["edges"] == 1
        assert client.post("/api/nodes/upsert", json=request).json()["replayed"]
        bad = {
            "nodes": [
                node("c").model_dump(),
                node(expected_revision="absent").model_dump(),
            ]
        }
        result = client.post("/api/nodes/upsert", json=bad)
        assert (
            result.status_code == 409 and result.json()["code"] == "revision_conflict"
        )
        rows = client.post(
            "/api/query", json={"cypher": "MATCH (n:Memory {id: 'c'}) RETURN n"}
        ).json()["rows"]
        assert rows == []
        bad = {"nodes": [], "operation_id": "rest"}
        result = client.post("/api/nodes/upsert", json=bad)
        assert (
            result.status_code == 409
            and result.json()["code"] == "operation_id_conflict"
        )


def test_database_scoped_receipts(graph, tmp_path):
    upsert_nodes(graph, graph.config, batch("shared-id"))
    with Engine(
        GragConfig(db_path=tmp_path / "other.lbdb", buffer_pool_size=128 * 1024**2)
    ) as other:
        other.execute_write(
            "CREATE NODE TABLE Memory(id STRING PRIMARY KEY, body STRING)"
        )
        result = upsert_nodes(
            other,
            other.config,
            UpsertNodesRequest(
                nodes=[node(body="other database")], operation_id="shared-id"
            ),
        )
        assert not result.replayed


def test_no_receipt_storage_for_ordinary_upserts(graph):
    upsert_nodes(graph, graph.config, batch())
    assert OPERATIONS_TABLE not in {
        r[0] for r in graph.execute("CALL SHOW_TABLES() RETURN name").rows
    }


def test_lost_commit_acknowledgement_replays_without_writing_again(graph, monkeypatch):
    real = graph.execute_write

    def lose_ack(query, params=None):
        result = real(query, params)
        if query == "COMMIT":
            raise RuntimeError("lost commit acknowledgement")
        return result

    monkeypatch.setattr(graph, "execute_write", lose_ack)
    with pytest.raises(RuntimeError, match="lost commit"):
        upsert_nodes(graph, graph.config, batch("uncertain"))
    monkeypatch.setattr(graph, "execute_write", real)
    assert upsert_nodes(graph, graph.config, batch("uncertain")).replayed
    assert graph.execute("MATCH ()-[r:SUPPORTS]->() RETURN count(r)").rows == [[1]]


@pytest.mark.parametrize("stage", ["node", "receipt"])
def test_process_crash_rolls_back_uncommitted_graph_and_receipt(graph, stage):
    config = graph.config
    graph.close()
    script = """
import os, sys
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.mutate import upsert_nodes
from grag.core.types import UpsertNode, UpsertNodesRequest
e=Engine(GragConfig(db_path=sys.argv[1],buffer_pool_size=128*1024**2))
real=e.execute_write
def crash(q,p=None):
    r=real(q,p)
    if (sys.argv[2]=='node' and q.startswith('MERGE (n')) or (sys.argv[2]=='receipt' and q.startswith('CREATE (o:_grag_operations')):
        os._exit(23)
    return r
e.execute_write=crash
upsert_nodes(e,e.config,UpsertNodesRequest(nodes=[UpsertNode(label='Memory',key='a',properties={'body':'interrupted'})],operation_id='crash'))
"""
    result = subprocess.run(  # noqa: S603 — fixed local crash worker
        [sys.executable, "-c", script, str(config.db_path), stage],
        env=os.environ.copy(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr.decode()
    with Engine(config) as reopened:
        assert body(reopened) == []
        result = upsert_nodes(
            reopened,
            config,
            UpsertNodesRequest(nodes=[node(body="interrupted")], operation_id="crash"),
        )
        assert not result.replayed


def test_failed_update_restores_fts_and_embedding_state(graph, monkeypatch):
    from grag.retrieval.search import _ensure_fts_index

    upsert_nodes(graph, graph.config, batch())
    graph.load_extension("FTS")
    _ensure_fts_index(graph, "Memory", "grag_fts__Memory", ["body"])
    graph.execute_write("ALTER TABLE Memory ADD embedding FLOAT[2]")
    graph.execute_write("MATCH (n:Memory) SET n.embedding = [1.0, 0.0]")
    real = graph.execute_write

    def fail(q, p=None):
        if q.startswith("MERGE (n") and p["key"] == "b":
            raise RuntimeError("after embedding invalidation")
        return real(q, p)

    monkeypatch.setattr(graph, "execute_write", fail)
    with pytest.raises(RuntimeError):
        upsert_nodes(
            graph,
            graph.config,
            UpsertNodesRequest(
                nodes=[node(body="changed"), node("b")], operation_id="failed-index"
            ),
        )
    assert graph.execute(
        "MATCH (n:Memory {id: 'a'}) RETURN n.body,n.embedding"
    ).rows == [["first", [1.0, 0.0]]]
    assert (
        graph.execute(
            "CALL QUERY_FTS_INDEX('Memory','grag_fts__Memory','changed') RETURN node.id"
        ).rows
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"nodes": [], "operation_id": ""},
        {"nodes": [], "operation_id": 123},
        {"nodes": [], "operation_id": "x" * 129},
        {"nodes": [], "operaton_id": "typo"},
        {"nodes": [{"label": "Memory", "key": "a", "expected_revison": "absent"}]},
        {"nodes": [{"label": "Memory", "key": "a", "expected_revision": "bad"}]},
    ],
)
def test_retry_and_guard_input_mistakes_are_not_silently_ignored(payload):
    with pytest.raises(ValidationError):
        UpsertNodesRequest.model_validate(payload)


def test_content_guard_detects_internal_source_edit(graph):
    upsert_nodes(graph, graph.config, batch())
    old = revision(graph)
    graph.execute_write("MATCH (n:Memory {id: 'a'}) SET n._source = 'new-location'")
    with pytest.raises(ConflictError):
        upsert_nodes(
            graph, graph.config, UpsertNodesRequest(nodes=[node(expected_revision=old)])
        )
