"""Hybrid search + get_context tests. FTS runs for real; the vector path is
driven by the deterministic FakeEmbedder from test_vectors (monkeypatched in).
"""

from __future__ import annotations

import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.errors import SchemaError
from grag.core.types import ContextRequest, SearchRequest
from grag.retrieval import vectors
from grag.retrieval.context import get_context
from grag.retrieval.search import search_knowledge
from grag.service import GragService

from test_vectors import FAKE_DIM, FakeEmbedder, make_docs


@pytest.fixture()
def docs_engine(tmp_path):
    eng = Engine(GragConfig(db_path=tmp_path / "retr.lbdb"))
    make_docs(eng)
    yield eng
    eng.close()


@pytest.fixture()
def hybrid_config(docs_engine, monkeypatch):
    fake = FakeEmbedder(synonyms={"automobile": "car"})
    monkeypatch.setattr(
        vectors,
        "get_embedder",
        lambda config: fake if config.embedder is not None else None,
    )
    return GragConfig(
        db_path=docs_engine.config.db_path,
        embedder=EmbedderConfig(provider="fastembed", model="fake", dim=FAKE_DIM),
    )


# --- FTS-only search ------------------------------------------------------------


def test_search_fts_only_finds_doc(docs_engine):
    cfg = docs_engine.config  # no embedder configured
    resp = search_knowledge(docs_engine, cfg, SearchRequest(query="graph relationships", hops=0))
    assert resp.seeds
    top = resp.seeds[0]
    assert top.node.id == "Doc:doc-0"
    assert top.match == "fts"
    assert {n.id for n in resp.subgraph.nodes} == {s.node.id for s in resp.seeds}
    assert isinstance(resp.context, str) and resp.context


def test_search_fts_no_match_returns_empty(docs_engine):
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="zzzzqqqq", hops=1)
    )
    assert resp.seeds == []
    assert resp.subgraph.nodes == []
    assert resp.context == ""


def test_search_fts_labels_filter(docs_engine):
    cfg = docs_engine.config
    resp = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph", labels=["Doc", "Ghost"], hops=0)
    )
    assert resp.seeds and all(s.node.label == "Doc" for s in resp.seeds)
    resp_none = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph", labels=["Ghost"], hops=0)
    )
    assert resp_none.seeds == []


# --- hybrid fusion ----------------------------------------------------------------


def test_hybrid_fusion_includes_vector_only_hits(docs_engine, hybrid_config):
    # 'doc-car' shares no literal token with the query, so FTS cannot find it;
    # the fake embedder maps 'automobile' -> 'car', making it a vector-only hit.
    docs_engine.execute_write(
        "CREATE (d:Doc {id: 'doc-car', title: 'car guide', "
        "text: 'car maintenance and repair basics'})"
    )
    resp = search_knowledge(
        docs_engine, hybrid_config, SearchRequest(query="automobile graph", top_k=4, hops=0)
    )
    by_id = {s.node.id: s for s in resp.seeds}
    assert "Doc:doc-car" in by_id
    assert by_id["Doc:doc-car"].match == "vector"
    assert "Doc:doc-0" in by_id  # lexical hit for 'graph'
    scores = [s.score for s in resp.seeds]
    assert scores == sorted(scores, reverse=True)


def test_search_vector_failure_degrades_to_fts(docs_engine, hybrid_config, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr("grag.retrieval.search.vector_candidates", boom)
    resp = search_knowledge(
        docs_engine, hybrid_config, SearchRequest(query="graph relationships", hops=0)
    )
    assert resp.seeds and resp.seeds[0].node.id == "Doc:doc-0"
    assert all(s.match == "fts" for s in resp.seeds)


# --- expansion --------------------------------------------------------------------


def test_search_hops_expansion(docs_engine):
    cfg = docs_engine.config
    r0 = search_knowledge(docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=0))
    r1 = search_knowledge(docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=1))
    r2 = search_knowledge(docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=2))
    assert {n.id for n in r0.subgraph.nodes} == {"Doc:doc-0"}
    assert {n.id for n in r1.subgraph.nodes} == {"Doc:doc-0", "Doc:doc-1"}
    assert {n.id for n in r2.subgraph.nodes} == {"Doc:doc-0", "Doc:doc-1", "Doc:doc-2"}
    assert any(e.type == "RELATED" for e in r1.subgraph.edges)
    assert r0.subgraph.edges == []


# --- get_context ------------------------------------------------------------------


def test_get_context_basic(docs_engine):
    resp = get_context(
        docs_engine, docs_engine.config, ContextRequest(node_ids=["Doc:doc-0"], hops=1)
    )
    ids = {n.id for n in resp.subgraph.nodes}
    assert {"Doc:doc-0", "Doc:doc-1"} <= ids
    assert not resp.truncated
    assert "Doc:doc-0" in resp.included_node_ids
    assert set(resp.included_node_ids) <= ids
    assert resp.token_estimate > 0
    assert resp.context


def test_get_context_missing_nodes_skipped(docs_engine):
    resp = get_context(
        docs_engine,
        docs_engine.config,
        ContextRequest(node_ids=["Doc:doc-0", "Doc:nope"], hops=0),
    )
    assert {n.id for n in resp.subgraph.nodes} == {"Doc:doc-0"}
    assert resp.included_node_ids == ["Doc:doc-0"]


def test_get_context_unknown_label_raises(docs_engine):
    with pytest.raises(SchemaError) as exc_info:
        get_context(docs_engine, docs_engine.config, ContextRequest(node_ids=["Ghost:1"]))
    assert "Doc" in str(exc_info.value)


def test_get_context_invalid_node_id_raises(docs_engine):
    with pytest.raises(SchemaError):
        get_context(docs_engine, docs_engine.config, ContextRequest(node_ids=["nocolon"]))


def test_get_context_budget_truncation(docs_engine):
    cfg = docs_engine.config
    ids = ["Doc:doc-0", "Doc:doc-1", "Doc:doc-2"]
    tight = get_context(docs_engine, cfg, ContextRequest(node_ids=ids, hops=0, token_budget=30))
    assert tight.truncated
    assert len(tight.included_node_ids) < 3
    room = get_context(docs_engine, cfg, ContextRequest(node_ids=ids, hops=0, token_budget=100_000))
    assert not room.truncated
    assert set(room.included_node_ids) == set(ids)


def test_get_context_empty_ids(docs_engine):
    resp = get_context(docs_engine, docs_engine.config, ContextRequest(node_ids=[], hops=1))
    assert resp.context == ""
    assert resp.subgraph.nodes == []
    assert not resp.truncated


# --- service integration ------------------------------------------------------------


def test_search_and_context_via_service(tmp_path, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(
        vectors,
        "get_embedder",
        lambda config: fake if config.embedder is not None else None,
    )
    cfg = GragConfig(
        db_path=tmp_path / "svc.lbdb",
        embedder=EmbedderConfig(provider="fastembed", model="fake", dim=FAKE_DIM),
    )
    svc = GragService(cfg)
    try:
        make_docs(svc.engine)
        resp = svc.search_knowledge(SearchRequest(query="semantic retrieval", hops=1))
        assert resp.seeds
        assert resp.seeds[0].node.id == "Doc:doc-1"
        assert {n.id for n in resp.subgraph.nodes} >= {"Doc:doc-0", "Doc:doc-1"}
        ctx = svc.get_context(ContextRequest(node_ids=["Doc:doc-2"], hops=5))  # clamped to max_hops
        assert "Doc:doc-2" in {n.id for n in ctx.subgraph.nodes}
    finally:
        svc.close()
