"""M13: work/admission bounds preserve atomicity and honest read outcomes."""
from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grag.api.main import create_app
from grag.code_state import scan_sources
from grag.config import GragConfig
from grag.core import limits
from grag.core.errors import ResourceLimitError
from grag.core.revisions import content_revision
from grag.core.types import (
    CodeIngestRequest,
    ContextRequest,
    QueryRequest,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.jobs import JobManager
from grag.mcp_server.server import create_server
from grag.registry import ServiceRegistry
from grag.retrieval.context import get_context
from grag.service import GragService
from test_evidence_lifecycle import node, setup, write


@pytest.mark.parametrize("model, args", [
    (SearchRequest, {"query": "x", "top_k": 65}),
    (SearchRequest, {"query": "x", "top_k": 0}),
    (SearchRequest, {"query": "x" * 8193}),
    (SearchRequest, {"query": "x", "labels": ["Note"] * 65}),
    (SearchRequest, {"query": "x", "token_budget": 32_769}),
    (ContextRequest, {"node_ids": ["Note:a"] * 65}),
    (QueryRequest, {"cypher": "x" * 65_537}),
    (CodeIngestRequest, {"paths": [], "max_file_kb": 32_769}),
])
def test_request_ranges(model, args):
    with pytest.raises(ValidationError):
        model(**args)


@pytest.mark.parametrize("query", [
    "UNWIND range(1,10000) AS x RETURN x LIMIT 10000",
    "UNWIND range(1,10000) AS x RETURN x UNION ALL RETURN 0 AS x",
])
def test_explicit_limit_and_union_cannot_over_materialize(tmp_path, monkeypatch, query):
    svc = GragService(GragConfig(db_path=tmp_path / "q.lbdb", auto_refresh_code=False))
    try:
        monkeypatch.setitem(limits.WORK_LIMITS, "result_rows", 50)
        response = svc.cypher_query(QueryRequest(cypher=query, limit=3))
        assert response.row_count == 3 and response.truncated
    finally:
        svc.close()


def test_work_failure_releases_reader_and_resets_next_request(engine, monkeypatch):
    monkeypatch.setitem(limits.WORK_LIMITS, "result_rows", 3)
    with pytest.raises(ResourceLimitError), limits.work_budget():
        engine.execute("UNWIND range(1,10) AS x RETURN x")
    with limits.work_budget():
        assert engine.execute("RETURN 1").rows == [[1]]


def test_aggregate_result_bytes_across_queries(engine, monkeypatch):
    monkeypatch.setitem(limits.WORK_LIMITS, "result_bytes", 100)
    with limits.work_budget():
        engine.execute("RETURN $text", {"text": "x" * 60})
        with pytest.raises(ResourceLimitError, match="result_bytes"):
            engine.execute("RETURN $text", {"text": "x" * 60})


def test_candidate_quota_is_shared_across_labels():
    assert limits.candidate_quota([str(i) for i in range(64)], 256) == 16
    assert limits.candidate_quota(["Note"], 256) == 256
    with pytest.raises(ResourceLimitError):
        limits.candidate_quota([str(i) for i in range(65)], 32)


def test_queue_rejects_before_record_or_callable_and_recovers():
    started, release = threading.Event(), threading.Event()
    manager = JobManager(max_pending=2, max_history=2)
    try:
        def blocked():
            started.set()
            assert release.wait(10)
            return {}
        first = manager.submit("test", blocked, {})
        assert started.wait(5)
        manager.submit("test", lambda: {}, {})
        with pytest.raises(ResourceLimitError, match="pending_jobs"):
            manager.submit("test", lambda: pytest.fail("rejected job ran"), {})
        assert len(manager.list()) == 2
        future = manager._futures[first.id]
        release.set()
        future.result(timeout=10)
    finally:
        release.set()
        manager.shutdown(wait=True)
    assert len(manager.list()) <= 2


def test_history_cap_rolls_back_content_sequence_and_receipt(engine, monkeypatch):
    from grag.core import evidence
    from grag.core.mutate import upsert_nodes
    from grag.core.types import EvidenceUpdate

    setup(engine)
    write(engine, "a", "original", evidence=EvidenceUpdate())
    before = node(engine, "a")
    monkeypatch.setattr(evidence, "MAX_HISTORY_PER_NODE", 1)
    with pytest.raises(ResourceLimitError, match="history_node_entries"):
        upsert_nodes(engine, engine.config, UpsertNodesRequest(operation_id="full-history", nodes=[
            UpsertNode(label="Note", key="a", properties={"body": "new"},
                       expected_revision=content_revision(before))]))
    assert node(engine, "a") == before
    monkeypatch.setattr(evidence, "MAX_HISTORY_PER_NODE", 2)
    result = upsert_nodes(engine, engine.config, UpsertNodesRequest(operation_id="full-history", nodes=[
        UpsertNode(label="Note", key="a", properties={"body": "new"}, expected_revision=content_revision(before))]))
    assert not result.replayed


def test_receipts_never_expire_and_capacity_rolls_back(engine, monkeypatch):
    from grag.core import mutations
    from grag.core.mutate import upsert_nodes

    setup(engine)
    request = UpsertNodesRequest(operation_id="first", nodes=[UpsertNode(label="Note", key="a")])
    upsert_nodes(engine, engine.config, request)
    monkeypatch.setattr(mutations, "MAX_RECEIPTS", 1)
    assert upsert_nodes(engine, engine.config, request).replayed
    with pytest.raises(ResourceLimitError, match="operation_receipts"):
        upsert_nodes(engine, engine.config, UpsertNodesRequest(operation_id="second", nodes=[UpsertNode(label="Note", key="b")]))
    assert engine.execute("MATCH (n:Note) RETURN n.id").rows == [["a"]]


def test_text_page_streams_unicode_without_unrelated_properties(engine, monkeypatch):
    setup(engine)
    text = "שלום 😀 a\n" * 20_000
    write(engine, "a", text)
    real_execute = engine.execute
    queries = []
    def execute(query, params=None, **kwargs):
        queries.append(query)
        return real_execute(query, params, **kwargs)
    monkeypatch.setattr(engine, "execute", execute)
    response = get_context(engine, engine.config, ContextRequest(node_ids=["Note:a"],
        text_property="body", text_offset=65_530, token_budget=700))
    assert response.text_page.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert response.text_page.total_chars == len(text)
    end = response.text_page.next_offset
    assert response.subgraph.nodes[0].properties["body"] == text[65_530:end]
    assert not any(q.split("}")[-1].strip() == "RETURN n" for q in queries)
    assert sum("substring(" in q for q in queries) >= 3


def test_source_limit_never_returns_partial_fingerprint(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / "b.py").write_text("b = 2\n")
    monkeypatch.setitem(limits.WORK_LIMITS, "source_bytes", 10)
    with pytest.raises(ResourceLimitError):
        scan_sources(tmp_path, CodeIngestRequest(paths=[str(tmp_path)]))


def test_http_body_bound_includes_chunked_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("grag.request_limits.MAX_REQUEST_BYTES", 100)
    app = create_app(GragConfig(db_path=tmp_path / "http.lbdb"))
    with TestClient(app) as client:
        response = client.post("/api/query", content=iter([b" " * 70, b" " * 70]))
        assert response.status_code == 413
        assert response.json()["resource"] == "request_bytes"


def test_mcp_rejects_oversize_before_sdk_validation(tmp_path, monkeypatch):
    monkeypatch.setattr("grag.mcp_server.server.MAX_REQUEST_BYTES", 100)
    registry = ServiceRegistry(GragConfig(db_path=tmp_path / "mcp.lbdb"))
    try:
        server = create_server(registry.config, registry=registry)
        result = asyncio.run(server.call_tool("search_knowledge", {"query": "x" * 150}))
        assert result.is_error and result.structured_content["code"] == "resource_limit"
    finally:
        registry.close()


def test_mutation_admission_is_atomic_before_schema_access(tmp_path):
    service = GragService(GragConfig(db_path=tmp_path / "write.lbdb"))
    try:
        with pytest.raises(ResourceLimitError, match="mutation_items"):
            service.upsert_nodes(UpsertNodesRequest(nodes=[UpsertNode(label="Missing", key=i) for i in range(1001)]))
        assert not service.describe_schema().node_tables
    finally:
        service.close()


def test_expansion_limit_is_aggregate_across_seeds(engine, monkeypatch):
    from grag.retrieval import search

    setup(engine)
    for key in ("a", "b", "c", "d"):
        write(engine, key, key)
    engine.execute_write("MATCH (a:Note {id:'a'}), (b:Note {id:'b'}), (c:Note {id:'c'}), (d:Note {id:'d'}) CREATE (a)-[:LINKS]->(b), (c)-[:LINKS]->(d)")
    monkeypatch.setattr(search, "MAX_EXPANSION_PATHS", 1)
    graph, limited = search._expand_neighborhood(engine, [("Note", "a"), ("Note", "c")], 1, {"Note": "id"})
    assert limited and len(graph.edges) == 1
    assert {n.id for n in graph.nodes} == {"Note:a", "Note:b"}


def test_lexical_and_packing_work_fail_explicitly(engine, monkeypatch):
    from grag.core.serialize import pack_context
    from grag.core.types import NodeRecord, ScoredNode, Subgraph
    from grag.retrieval.lexical import rank_lexical

    candidates = [ScoredNode(node=NodeRecord(id=f"{label}:a", label=label, properties={"body": "word " * 50}),
                             score=1, match="fts") for label in ("A", "B")]
    monkeypatch.setitem(limits.WORK_LIMITS, "lexical_bytes", 300)
    with pytest.raises(ResourceLimitError, match="lexical_bytes"):
        rank_lexical(engine, candidates, "word", {"A": ["body"], "B": ["body"]})
    monkeypatch.setitem(limits.WORK_LIMITS, "packing_steps", 1)
    with pytest.raises(ResourceLimitError, match="packing_steps"):
        pack_context(Subgraph(nodes=[s.node for s in candidates]), 20)


def test_active_admission_recovers_after_operation_exit(tmp_path, monkeypatch):
    from grag import service as module

    svc = GragService(GragConfig(db_path=tmp_path / "active.lbdb"))
    entered, release = threading.Event(), threading.Event()
    def active():
        with svc.operation():
            entered.set()
            assert release.wait(5)
    thread = threading.Thread(target=active)
    monkeypatch.setattr(module, "MAX_ACTIVE_OPERATIONS", 1)
    try:
        thread.start()
        assert entered.wait(5)
        with pytest.raises(ResourceLimitError, match="active_operations"), svc.operation():
            pytest.fail("admitted beyond capacity")
        release.set()
        thread.join(5)
        with svc.operation(), svc.operation():
            pass  # nested calls count as one admitted operation
    finally:
        release.set()
        thread.join(5)
        svc.close()


def test_source_capacity_failure_is_not_fresh(tmp_path, monkeypatch):
    from grag.core.errors import FreshnessError
    from grag.core.types import ReadPolicy

    root = tmp_path / "code"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    svc = GragService(GragConfig(db_path=tmp_path / "fresh.lbdb"))
    try:
        svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
        monkeypatch.setitem(limits.WORK_LIMITS, "source_bytes", 1)
        svc.enable_auto_refresh()
        with pytest.raises(FreshnessError) as error:
            svc.read_freshness(ReadPolicy(freshness="require", freshness_timeout_ms=300))
        assert error.value.freshness["status"] != "fresh"
        assert "Resource limit" in str(svc.refresh_status(detail=True))
    finally:
        svc.close()


def test_document_path_loading_has_aggregate_bound(tmp_path, monkeypatch):
    from grag.ingest.loaders import load_paths

    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("x" * 70)
    monkeypatch.setitem(limits.WORK_LIMITS, "source_document_bytes", 100)
    with pytest.raises(ResourceLimitError, match="source_document_bytes"):
        load_paths([tmp_path])


def test_response_size_error_has_matching_rest_mcp_code(tmp_path, monkeypatch):
    import grag.service as module

    app = create_app(GragConfig(db_path=tmp_path / "response.lbdb", auto_refresh_code=False))
    server = create_server(app.state.registry.config, registry=app.state.registry)
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 500)
    with TestClient(app) as client:
        args = {"cypher": "RETURN repeat('a', 2000)"}
        rest = client.post("/api/query", json=args)
        mcp = asyncio.run(server.call_tool("cypher_query", args))
        assert rest.status_code == 413
        assert rest.json()["code"] == mcp.structured_content["code"] == "resource_limit"
        assert mcp.is_error


def test_native_buffer_exhaustion_has_resource_guidance(engine, monkeypatch):
    def exhaust(*args):
        raise RuntimeError("Buffer manager exception: Unable to allocate memory! The buffer pool is full and no memory could be freed!")
    monkeypatch.setattr(engine._write_conn, "execute", exhaust)
    with pytest.raises(ResourceLimitError) as error:
        engine.execute_write("RETURN 1")
    assert error.value.resource == "native_buffer_bytes"
    assert "GRAG_BUFFER_POOL_MB" in error.value.hint


def test_multiple_statements_are_rejected_but_quoted_semicolons_work(tmp_path):
    from grag.core.errors import SchemaError

    svc = GragService(GragConfig(db_path=tmp_path / "statements.lbdb", auto_refresh_code=False))
    try:
        with pytest.raises(SchemaError, match="one statement"):
            svc.cypher_query(QueryRequest(cypher="RETURN 1; RETURN 2"))
        assert svc.cypher_query(QueryRequest(cypher="RETURN ';' /* ; */;")).rows == [[";"]]
    finally:
        svc.close()


def test_history_cannot_commit_an_unpageable_metadata_entry(engine):
    from grag.core.types import EvidenceUpdate

    setup(engine)
    with pytest.raises(ResourceLimitError, match="history_metadata_bytes"):
        write(engine, "a", "body", source="a" * 20_000, evidence=EvidenceUpdate())
    assert engine.execute("MATCH (n:Note) RETURN count(n)").rows == [[0]]


def test_job_record_rejected_before_admission():
    manager = JobManager()
    try:
        with pytest.raises(ResourceLimitError, match="job_record_bytes"):
            manager.submit("test", lambda: pytest.fail("must not run"), {"path": "a" * limits.MAX_RESPONSE_BYTES})
        assert not manager.list()
    finally:
        manager.shutdown(wait=True)


def test_oversized_job_result_becomes_a_readable_failure():
    from concurrent.futures import Future

    manager = JobManager()
    finished = threading.Event()
    original = manager._finished
    def done(job_id: str, future: Future):
        original(job_id, future)
        finished.set()
    manager._finished = done
    try:
        job = manager.submit("test", lambda: {"value": "a" * limits.MAX_RESPONSE_BYTES}, {"note": "metadata"})
        assert finished.wait(5)
        result = manager.get(job.id)
        assert result.status == "failed" and "job_result_bytes" in result.error
        assert len(result.model_dump_json().encode()) <= limits.MAX_RESPONSE_BYTES
    finally:
        manager.shutdown(wait=True)


def test_error_envelopes_bound_large_names_and_native_messages():
    import json

    from grag.core.errors import GragError, validation_error_body

    value = "\x00" * limits.MAX_REQUEST_BYTES
    body = validation_error_body([{"loc": ("nodes", value), "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}])
    assert "[truncated]" in body["details"][0]["loc"][1]
    assert len(json.dumps(body).encode()) < 32_768
    body = GragError(value, hint=value).to_dict()
    assert len(json.dumps(body).encode()) < 65_536
