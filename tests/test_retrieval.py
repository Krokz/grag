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
    resp = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph relationships", hops=0)
    )
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
    assert resp.pending_embeddings == 0


def test_search_reports_and_drains_pending_embeddings(docs_engine, hybrid_config):
    """In-request embedding is capped; the response surfaces the backlog so
    callers know vector recall improves as later searches drain it."""
    hybrid_config.max_embed_per_search = 2
    resp = search_knowledge(
        docs_engine, hybrid_config, SearchRequest(query="graph relationships", hops=0)
    )
    assert resp.pending_embeddings == 1
    resp = search_knowledge(
        docs_engine, hybrid_config, SearchRequest(query="graph relationships", hops=0)
    )
    assert resp.pending_embeddings == 0


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
        docs_engine,
        hybrid_config,
        SearchRequest(query="automobile graph", top_k=4, hops=0),
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


# --- per-label diversity cap ------------------------------------------------------


def _make_flood(eng: Engine) -> None:
    """A big 'Function' table that lexically floods the query, plus one
    'Decision' node that is the semantically intended answer."""
    eng.execute_write(
        "CREATE NODE TABLE Func(id STRING PRIMARY KEY, name STRING, doc STRING)"
    )
    eng.execute_write(
        "CREATE NODE TABLE Decision(id STRING PRIMARY KEY, title STRING, body STRING)"
    )
    for i in range(6):
        eng.execute_write(
            "CREATE (f:Func {id: $id, name: $n, doc: $d})",
            {
                "id": f"fn-{i}",
                "n": f"python_backend_helper_{i}",
                "d": "python backend python backend utility",
            },
        )
    eng.execute_write(
        "CREATE (d:Decision {id: 'why-python', title: 'why python backend', "
        "body: 'python backend chosen for pydantic and embeddings'})"
    )


def test_label_cap_surfaces_underrepresented_label(tmp_path):
    eng = Engine(GragConfig(db_path=tmp_path / "cap.lbdb", search_label_cap=2))
    try:
        _make_flood(eng)
        resp = search_knowledge(
            eng, eng.config, SearchRequest(query="python backend", top_k=4, hops=0)
        )
        labels = [s.node.label for s in resp.seeds]
        assert "Decision" in labels  # the lone knowledge node surfaces
        # within the first `cap` positions per label, Func holds at most 2
        first_pass = labels[: 2 + 1]  # 2 Func slots + next label's first slot
        assert first_pass.count("Func") <= 2
        # top of the ranking is not exclusively code
        assert not all(lbl == "Func" for lbl in labels[:3])
        assert len(resp.seeds) == 4
    finally:
        eng.close()


def test_label_cap_zero_disables_diversity(tmp_path):
    eng = Engine(GragConfig(db_path=tmp_path / "nocap.lbdb", search_label_cap=0))
    try:
        _make_flood(eng)
        resp = search_knowledge(
            eng, eng.config, SearchRequest(query="python backend", top_k=4, hops=0)
        )
        assert all(s.node.label == "Func" for s in resp.seeds)  # pure RRF flood
    finally:
        eng.close()


# --- expansion --------------------------------------------------------------------


def test_search_hops_expansion(docs_engine):
    cfg = docs_engine.config
    r0 = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=0)
    )
    r1 = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=1)
    )
    r2 = search_knowledge(
        docs_engine, cfg, SearchRequest(query="graph relationships", top_k=1, hops=2)
    )
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
        get_context(
            docs_engine, docs_engine.config, ContextRequest(node_ids=["Ghost:1"])
        )
    assert "Doc" in str(exc_info.value)


def test_get_context_invalid_node_id_raises(docs_engine):
    with pytest.raises(SchemaError):
        get_context(
            docs_engine, docs_engine.config, ContextRequest(node_ids=["nocolon"])
        )


def test_get_context_budget_truncation(docs_engine):
    cfg = docs_engine.config
    docs_engine.execute_write(
        "MATCH (d:Doc {id: 'doc-0'}) SET d.text = $text", {"text": "long memory " * 100}
    )
    ids = ["Doc:doc-0", "Doc:doc-1", "Doc:doc-2"]
    tight = get_context(
        docs_engine, cfg, ContextRequest(node_ids=ids, hops=0, token_budget=256)
    )
    assert tight.truncated
    assert tight.omitted_nodes or tight.omitted_properties
    assert tight.response_token_estimate <= 256
    room = get_context(
        docs_engine, cfg, ContextRequest(node_ids=ids, hops=0, token_budget=32_768)
    )
    assert not room.truncated
    assert set(room.included_node_ids) == set(ids)


def test_get_context_empty_ids(docs_engine):
    resp = get_context(
        docs_engine, docs_engine.config, ContextRequest(node_ids=[], hops=1)
    )
    assert resp.context == ""
    assert resp.subgraph.nodes == []


def test_get_context_accepts_bare_code_graph_ids(engine, tmp_path):
    """Code-graph node keys are 'repo:path#qual' (no 'Label:' prefix), which
    split_node_id would misread as label='repo'. get_context must resolve the
    bare id to its table server-side. Regression for the dogfooding papercut."""
    from grag.core.types import CodeIngestRequest
    from grag.ingest.code import _repo_id, ingest_code

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text("def helper(x):\n    return x * 2\n", encoding="utf-8")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    bare = f"{_repo_id(pkg)}:core.py#helper"  # no Label prefix
    prefixed = f"Function:{bare}"
    r_bare = get_context(engine, engine.config, ContextRequest(node_ids=[bare], hops=0))
    r_prefixed = get_context(
        engine, engine.config, ContextRequest(node_ids=[prefixed], hops=0)
    )
    assert prefixed in r_bare.included_node_ids
    assert r_bare.included_node_ids == r_prefixed.included_node_ids


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
        ctx = svc.get_context(
            ContextRequest(node_ids=["Doc:doc-2"], hops=5)
        )  # clamped to max_hops
        assert "Doc:doc-2" in {n.id for n in ctx.subgraph.nodes}
    finally:
        svc.close()


# --- per-label candidate counts (one-lookup stop rule for memory capture) ----------


def test_search_reports_label_hits_and_footer(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0)
    )
    assert resp.label_hits.get("Doc", 0) >= 1
    assert all(isinstance(v, int) and v >= 0 for v in resp.label_hits.values())
    assert '"label_hits":{' in mcp_retrieval_text(resp)


def test_search_label_hits_requested_labels_report_zero(docs_engine):
    # Requested labels are always listed, zeros included, so one lookup can
    # establish that no memory record matched.
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="zzzzqqqq", hops=0, labels=["Doc"])
    )
    assert resp.label_hits == {"Doc": 0}
    # An unrestricted search lists only labels with hits, bounded to eight entries.
    resp = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query="zzzzqqqq", hops=0))
    assert resp.label_hits == {}


def test_search_unknown_requested_labels_are_explicit(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    # A requested label with no table is reported distinctly from a zero match:
    # the scope itself is wrong (typo or wrong database), so do not repeat it.
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=["Doc", "Nope"])
    )
    assert resp.unknown_labels == ["Nope"]
    assert "Nope" not in resp.label_hits
    assert resp.label_hits.get("Doc", 0) >= 1
    assert '"unknown_labels":["Nope"]' in mcp_retrieval_text(resp)


def test_search_unknown_only_labels_return_empty_with_status(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=["Nope"])
    )
    assert resp.unknown_labels == ["Nope"]
    assert resp.label_hits == {} and resp.seeds == []
    assert resp.response_token_estimate <= 8192
    assert "unknown_labels" in mcp_retrieval_text(resp)
    # An unrestricted search never reports unknown labels.
    resp = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0))
    assert resp.unknown_labels == []
    assert "unknown_labels" not in mcp_retrieval_text(resp)


def test_search_unknown_labels_are_budget_trimmed_with_explicit_status(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    # Eight 128-character unknown labels must not break a 256-token budget;
    # the wrong-scope signal survives as an explicit omitted count.
    labels = [f"L{i}" + "x" * 126 for i in range(8)]
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=labels, token_budget=256)
    )
    assert resp.response_token_estimate <= 256
    assert len(mcp_retrieval_text(resp).encode()) <= 1024
    assert len(resp.model_dump_json().encode()) <= 1024
    assert len(resp.unknown_labels) + resp.unknown_labels_omitted == 8
    assert resp.unknown_labels_omitted >= 1
    assert "unknown_labels" in mcp_retrieval_text(resp)  # names or their count


def test_search_unknown_labels_fit_default_budget_at_max_request(docs_engine):
    labels = [f"L{i:02d}" + "x" * 124 for i in range(64)]  # 64 unknown 128-char labels
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=labels)
    )
    assert resp.response_token_estimate <= 2000  # default budget
    assert len(resp.unknown_labels) + resp.unknown_labels_omitted == 64


def test_search_unknown_labels_never_displace_evidence(docs_engine):
    # Evidence is packed against the metadata-free floor; unknown labels only
    # use leftover space, like label counts.
    base = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=["Doc"], token_budget=512)
    )
    with_unknown = search_knowledge(
        docs_engine, docs_engine.config,
        SearchRequest(query="graph relationships", hops=0, labels=["Doc", *[f"U{i}" + "x" * 126 for i in range(8)]], token_budget=512),
    )
    assert with_unknown.included_node_ids == base.included_node_ids
    assert with_unknown.truncated == base.truncated
    assert with_unknown.unknown_labels or with_unknown.unknown_labels_omitted


# --- guard revisions in ordinary reads (September 2026 longitudinal audit) -------


def test_reads_expose_guard_revisions_matching_cypher(docs_engine):
    from grag.core.revisions import annotate_revisions
    from grag.retrieval.context import get_context

    # Reference tokens from the Cypher path — formerly the only way to read them.
    reference = {}
    for (row,) in docs_engine.execute("MATCH (n:Doc) RETURN n").rows:
        annotated = annotate_revisions(row)
        reference[f"Doc:{annotated['id']}"] = annotated["_revision"]

    resp = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=1))
    assert resp.seeds and len(resp.subgraph.nodes) > 1
    for node in resp.subgraph.nodes:
        assert node.properties["_revision"] == reference[node.id]
    ctx = get_context(docs_engine, docs_engine.config, ContextRequest(node_ids=["Doc:doc-0"], hops=0))
    assert ctx.subgraph.nodes[0].properties["_revision"] == reference["Doc:doc-0"]


def test_read_revision_token_satisfies_the_upsert_guard(docs_engine):
    from grag.core.errors import ConflictError
    from grag.core.mutate import upsert_nodes
    from grag.core.types import UpsertNode, UpsertNodesRequest
    from grag.retrieval.context import get_context

    ctx = get_context(docs_engine, docs_engine.config, ContextRequest(node_ids=["Doc:doc-1"], hops=0))
    token = ctx.subgraph.nodes[0].properties["_revision"]
    # A guarded write with the read token succeeds without any Cypher detour.
    summary = upsert_nodes(docs_engine, docs_engine.config, UpsertNodesRequest(nodes=[
        UpsertNode(label="Doc", key="doc-1", properties={"title": "vector search, corrected"}, expected_revision=token)
    ]))
    assert summary.revisions["Doc:doc-1"] != token
    # The pre-write token is now stale and must conflict.
    try:
        upsert_nodes(docs_engine, docs_engine.config, UpsertNodesRequest(nodes=[
            UpsertNode(label="Doc", key="doc-1", properties={"title": "stale"}, expected_revision=token)
        ]))
        raise AssertionError("stale token accepted")
    except ConflictError:
        pass


def test_revision_survives_budget_pressure(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    docs_engine.execute_write(
        "CREATE (d:Doc {id: $id, title: $t, text: $x})",
        {"id": "long-0", "t": "graph relationships long", "x": "graph relationships " + "x" * 800},
    )
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships long", hops=0, token_budget=320)
    )
    node = next(n for n in resp.subgraph.nodes if n.id == "Doc:long-0")
    assert "text" not in node.properties  # the body gave way
    assert node.properties["_revision"]  # the guard token did not
    assert "_revision:" in mcp_retrieval_text(resp)


def test_surface_threads_through_search_and_context(docs_engine):
    from grag.retrieval.context import get_context
    from grag.retrieval.packing import estimate_tokens, mcp_retrieval_text

    search_req = SearchRequest(query="graph relationships", hops=1, token_budget=4096)
    rest = search_knowledge(docs_engine, docs_engine.config, search_req)
    mcp = search_knowledge(docs_engine, docs_engine.config, search_req, surface="mcp")
    assert rest.response_token_estimate == max(
        estimate_tokens(rest.model_dump_json()), estimate_tokens(mcp_retrieval_text(rest))
    )
    assert mcp.response_token_estimate == estimate_tokens(mcp_retrieval_text(mcp))

    ctx_req = ContextRequest(node_ids=["Doc:doc-0"], hops=1, token_budget=4096)
    rest = get_context(docs_engine, docs_engine.config, ctx_req)
    mcp = get_context(docs_engine, docs_engine.config, ctx_req, surface="mcp")
    assert rest.response_token_estimate == max(
        estimate_tokens(rest.model_dump_json()), estimate_tokens(mcp_retrieval_text(rest))
    )
    assert mcp.response_token_estimate == estimate_tokens(mcp_retrieval_text(mcp))


def test_search_label_hits_are_distinct_nodes_across_modalities(docs_engine, hybrid_config):
    resp = search_knowledge(docs_engine, hybrid_config, SearchRequest(query="graph relationships", hops=0))
    matches = {s.match for s in resp.seeds}
    assert resp.label_hits.get("Doc", 0) <= 3  # three stored docs; FTS and vector hits are not summed
    assert resp.label_hits["Doc"] >= len({s.node.id for s in resp.seeds})
    assert matches  # hybrid search ran


def test_search_label_hits_bounded_for_unrestricted_search(docs_engine, monkeypatch):
    from grag.retrieval import search as search_module

    monkeypatch.setattr(search_module, "LABEL_HITS_LIMIT", 0)
    resp = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0))
    assert resp.seeds and resp.label_hits == {} and resp.label_hits_omitted == 1


def test_search_label_hits_explicit_labels_are_bounded_with_zeros_first(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    for i in range(10):
        docs_engine.execute_write(f"CREATE NODE TABLE Extra{i}(id STRING PRIMARY KEY, text STRING)")
    docs_engine.execute_write("CREATE (n:Extra3 {id: 'x', text: 'graph relationships here'})")
    labels = ["Doc", *[f"Extra{i}" for i in range(10)]]
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=labels, token_budget=256)
    )
    # Eleven requested labels: as many listed as leftover space allows after
    # evidence (now including the citation-class _revision), zeros first, the
    # rest counted as omitted. The 8/3 split became 6/5 when _revision joined
    # the citation bundle; the budget and ordering invariants are unchanged.
    assert len(resp.label_hits) == 6 and resp.label_hits_omitted == 5
    values = list(resp.label_hits.values())
    assert values == sorted(values, key=lambda v: (v != 0, -v))
    footer = mcp_retrieval_text(resp).split("\n---\n")[-1]
    assert '"label_hits_omitted":5' in footer
    assert resp.response_token_estimate <= 256


def test_search_label_hits_are_trimmed_before_the_budget_is_exceeded(docs_engine):
    from grag.retrieval.packing import mcp_retrieval_text

    labels = []
    for i in range(8):
        name = f"L{i}" + "x" * 126  # eight valid 128-character labels
        labels.append(name)
        docs_engine.execute_write(f"CREATE NODE TABLE {name}(id STRING PRIMARY KEY, text STRING)")
        docs_engine.execute_write(f"CREATE (n:{name} {{id: 'x', text: 'graph relationships'}})")
    resp = search_knowledge(
        docs_engine, docs_engine.config, SearchRequest(query="graph relationships", hops=0, labels=labels, token_budget=256)
    )
    # Counts are optional: they are dropped, each counted as omitted, so the
    # complete response respects the budget and the 1,024-byte ceiling it implies.
    assert resp.response_token_estimate <= 256
    assert len(mcp_retrieval_text(resp).encode()) <= 1024
    assert len(resp.model_dump_json().encode()) <= 1024
    assert len(resp.label_hits) + resp.label_hits_omitted == 8
    assert resp.label_hits_omitted >= 1


def test_search_label_hits_never_displace_evidence(docs_engine, monkeypatch):
    """Evidence is packed against the count-free floor; counts only use leftover space."""
    from grag.retrieval import search as search_module

    for i in range(3):
        docs_engine.execute_write(f"CREATE NODE TABLE Extra{i}(id STRING PRIMARY KEY, text STRING)")
        docs_engine.execute_write(f"CREATE (n:Extra{i} {{id: 'x{i}', text: 'graph relationships note {i}'}})")
    req = {"query": "graph relationships", "hops": 0, "top_k": 8}
    roomy = search_knowledge(docs_engine, docs_engine.config, SearchRequest(**req, token_budget=8192))
    assert len(roomy.included_node_ids) >= 4 and len(roomy.label_hits) == 4
    # The same reply with every count dropped fixes the budget at exactly its size.
    monkeypatch.setattr(search_module, "LABEL_HITS_LIMIT", 0)
    floor = search_knowledge(docs_engine, docs_engine.config, SearchRequest(**req, token_budget=8192))
    monkeypatch.setattr(search_module, "LABEL_HITS_LIMIT", 8)
    budget = floor.response_token_estimate
    tight = search_knowledge(docs_engine, docs_engine.config, SearchRequest(**req, token_budget=budget))
    assert tight.response_token_estimate <= budget
    # All evidence that fit without counts still fits; counts gave way instead.
    assert tight.included_node_ids == floor.included_node_ids
    assert tight.truncated == floor.truncated
    assert len(tight.label_hits) + tight.label_hits_omitted == 4
    assert tight.label_hits_omitted >= 1


# --- exact identifier promotion (frozen rule, September 2026) ----------------------


def _scored(nid, name, label="Function"):
    from grag.core.types import NodeRecord, ScoredNode

    return ScoredNode(node=NodeRecord(id=nid, label=label, properties={"name": name}), score=0.01, match="fts")


def test_identifier_tokens_recognition():
    from grag.retrieval.search import identifier_tokens

    assert identifier_tokens("what does stamp_message_id write into messages") == {"stamp_message_id"}
    assert identifier_tokens("when is ScopeError raised by RepositoryScope") == {"ScopeError", "RepositoryScope"}
    assert identifier_tokens("what does _now return") == {"_now"}
    # Capitalized ordinary words, single-hump words and plain words never qualify.
    assert identifier_tokens("Does Lumio Triage Retry Failed Attachment Downloads") == set()
    assert identifier_tokens("how are ticket attachments cleaned up after triage") == set()
    assert identifier_tokens("Xml") == set() and identifier_tokens("XmlParser") == {"XmlParser"}


def test_exact_promotion_preserves_fused_order_and_leaves_others_in_place():
    from grag.retrieval.search import _promote_exact_identifiers

    fused = [_scored("F:a", "alpha"), _scored("F:b", "run_triage"), _scored("F:c", "gamma"), _scored("F:d", "run_triage"), _scored("F:e", "delta")]
    out = _promote_exact_identifiers(fused, "how does run_triage build its response")
    # Duplicate names are all promoted, in their fused order; the rest keep their order.
    assert [s.node.id for s in out] == ["F:b", "F:d", "F:a", "F:c", "F:e"]
    assert _promote_exact_identifiers(fused, "how does triage build its response") is fused
    assert _promote_exact_identifiers(fused, "what does zeta_missing do") is fused


def test_exact_promotion_window_boundary():
    from grag.retrieval.search import EXACT_MATCH_WINDOW, _promote_exact_identifiers

    filler = [_scored(f"F:{i}", f"node{i}") for i in range(EXACT_MATCH_WINDOW - 1)]
    at_64 = _promote_exact_identifiers([*filler, _scored("F:x", "scrub_pii")], "what does scrub_pii remove")
    assert at_64[0].node.id == "F:x"
    at_65 = _promote_exact_identifiers([*filler, _scored("F:pad", "pad"), _scored("F:x", "scrub_pii")], "what does scrub_pii remove")
    assert [s.node.id for s in at_65][-1] == "F:x" and at_65[0].node.id == "F:0"


def test_exact_promotion_changes_ordering_before_diversity_and_packing(docs_engine, monkeypatch):
    from grag.retrieval import search as search_module

    docs_engine.execute_write("CREATE NODE TABLE Fn(id STRING PRIMARY KEY, name STRING, text STRING)")
    for i in range(4):
        docs_engine.execute_write(
            f"CREATE (n:Fn {{id: 'fn{i}', name: 'helper{i}', "
            f"text: 'build graph index relationships build graph index relationships helper {i}'}})"
        )
    docs_engine.execute_write("CREATE (n:Fn {id: 'target', name: 'build_graph_index', text: 'nothing here'})")
    req = SearchRequest(query="build graph index relationships build_graph_index", hops=0, top_k=4)
    promoted = search_knowledge(docs_engine, docs_engine.config, req)
    monkeypatch.setattr(search_module, "_promote_exact_identifiers", lambda fused, query: fused)
    plain = search_knowledge(docs_engine, docs_engine.config, req)
    # Same query, same candidates: lexical order leaves the exact name out of the top seeds;
    # promotion makes it the first seed, ahead of diversity and packing.
    assert "Fn:target" not in [s.node.id for s in plain.seeds]
    assert promoted.seeds[0].node.id == "Fn:target"
    # Only the exact name moved: every other promoted seed was already a plain seed
    # (diversity then re-applies its per-label cap to the shifted list).
    assert {s.node.id for s in promoted.seeds[1:]} <= {s.node.id for s in plain.seeds}


def test_exact_promotion_does_not_guarantee_delivery(docs_engine):
    long_id = "target" + "y" * 1200
    docs_engine.execute_write("CREATE NODE TABLE Fn(id STRING PRIMARY KEY, name STRING, text STRING)")
    for i in range(4):
        docs_engine.execute_write(f"CREATE (n:Fn {{id: 'fn{i}', name: 'helper{i}', text: 'graph relationships helper {i}'}})")
    docs_engine.execute_write(f"CREATE (n:Fn {{id: '{long_id}', name: 'build_graph_index', text: 'graph'}})")
    query = "graph relationships build_graph_index"
    roomy = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query=query, hops=0, top_k=4, token_budget=8192))
    assert roomy.seeds[0].node.id == f"Fn:{long_id}"  # promoted to the front of the fused list
    tight = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query=query, hops=0, top_k=4, token_budget=256))
    # The promoted record cannot fit the budget: it is omitted, not delivered, and the reply stays within budget.
    assert f"Fn:{long_id}" not in tight.included_node_ids
    assert tight.omitted_nodes >= 1 and tight.truncated
    assert tight.response_token_estimate <= 256


def test_exact_promotion_ignores_non_string_names(docs_engine):
    docs_engine.execute_write("CREATE NODE TABLE Tagged(id STRING PRIMARY KEY, name STRING[], text STRING)")
    docs_engine.execute_write("CREATE (n:Tagged {id: 't1', name: ['foo_bar', 'baz'], text: 'foo_bar text'})")
    resp = search_knowledge(docs_engine, docs_engine.config, SearchRequest(query="foo_bar", hops=0, top_k=4))
    assert [s.node.id for s in resp.seeds] == ["Tagged:t1"]  # returned normally, never promoted by a list name
