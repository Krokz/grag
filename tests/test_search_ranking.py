"""M05: judged cross-label recall, permutation invariance and modality fusion."""

from __future__ import annotations

import itertools
import math

import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.types import NodeRecord, ScoredNode, SearchRequest
from grag.retrieval import vectors
from grag.retrieval.search import _diversify, _fts_seeds, _rrf_fuse, search_knowledge
from test_vectors import FAKE_DIM, FakeEmbedder


def _table(engine, label):
    engine.execute_write(
        f"CREATE NODE TABLE {label}(id STRING PRIMARY KEY, title STRING, text STRING, _source STRING)"
    )


def _put(engine, label, key, title, text, source="ranking-fixture"):
    engine.execute_write(
        f"CREATE (n:{label} {{id: $id, title: $title, text: $text, _source: $source}})",
        {"id": key, "title": title, "text": text, "source": source},
    )


def _signature(response):
    return [(s.node.id, s.score, s.match) for s in response.seeds]


@pytest.fixture()
def ranked_engine(tmp_path):
    engine = Engine(
        GragConfig(db_path=tmp_path / "ranking.lbdb", buffer_pool_size=128 * 1024**2)
    )
    # Unequal table sizes: code is abundant; decisions and open tasks are scarce.
    for label in ["Function", "Decision", "Task"]:
        _table(engine, label)
    for i in range(40):
        _put(
            engine,
            "Function",
            f"helper-{i:02d}",
            "backend helper",
            "python backend utility " * 3,
        )
    _put(
        engine,
        "Decision",
        "python",
        "Why choose Python backend",
        "We chose Python for pydantic validation and local embeddings.",
    )
    _put(
        engine,
        "Decision",
        "storage",
        "Database location",
        "Keep the graph database outside OneDrive to avoid sync conflicts.",
    )
    _put(
        engine,
        "Task",
        "resume",
        "Resume interrupted ingest",
        "Next session: make interrupted ingest retry preserve authored memories.",
    )
    _put(
        engine,
        "Task",
        "done",
        "Completed ingestion work",
        "Completed an old unrelated import prototype.",
    )
    _put(
        engine,
        "Function",
        "retry",
        "retry_connection",
        "Reconnect connections after the upstream server restarts.",
    )
    _put(engine, "Function", "orchid", "", "orchid " + "unrelated " * 100)
    _put(engine, "Decision", "orchid", "", "orchid")
    _put(engine, "Task", "noise", "Routine chores", "unrelated text")
    try:
        yield engine
    finally:
        engine.close()


# These are relevance judgments for small agent workflows, not a benchmark of
# real embedding models. Each expected first hit contains the requested answer.
JUDGMENTS = [
    ("why choose Python backend", "Decision:python"),
    ("graph database outside OneDrive", "Decision:storage"),
    ("next session interrupted ingest retry", "Task:resume"),
    ("reconnecting connection upstream server", "Function:retry"),
    ("orchid", "Decision:orchid"),
]


@pytest.mark.parametrize(("query", "expected"), JUDGMENTS)
@pytest.mark.parametrize("cap", [0, 1, 2])
def test_judged_relevance_is_independent_of_label_order(
    ranked_engine, query, expected, cap
):
    cfg = ranked_engine.config.model_copy(update={"search_label_cap": cap})
    reference = None
    for labels in itertools.permutations(["Function", "Decision", "Task"]):
        response = search_knowledge(
            ranked_engine,
            cfg,
            SearchRequest(query=query, labels=list(labels), top_k=4, hops=0),
        )
        assert response.seeds[0].node.id == expected
        signature = _signature(response)
        assert signature == reference if reference is not None else True
        reference = signature
    for labels in [None, [], ["Ghost", "Task", "Decision", "Function", "Decision"]]:
        response = search_knowledge(
            ranked_engine,
            cfg,
            SearchRequest(query=query, labels=labels, top_k=4, hops=0),
        )
        assert _signature(response) == reference


def test_original_audit_single_result_prefers_concise_hit_in_either_order(tmp_path):
    engine = Engine(GragConfig(db_path=tmp_path / "audit.lbdb"))
    try:
        for label in ["Alpha", "Beta"]:
            _table(engine, label)
        _put(engine, "Alpha", "weak", "", "orchid " + "irrelevant " * 80)
        _put(engine, "Beta", "strong", "", "orchid")
        for labels in [["Alpha", "Beta"], ["Beta", "Alpha"]]:
            response = search_knowledge(
                engine,
                engine.config,
                SearchRequest(query="orchid", labels=labels, top_k=1, hops=0),
            )
            assert response.seeds[0].node.id == "Beta:strong"
    finally:
        engine.close()


def test_local_bm25_values_from_unequal_corpora_are_not_compared_directly(tmp_path):
    engine = Engine(GragConfig(db_path=tmp_path / "idf.lbdb", search_label_cap=0))
    try:
        for label in ["Large", "Small"]:
            _table(engine, label)
        for i in range(60):
            _put(engine, "Large", f"noise-{i}", "", "irrelevant " * 80)
        _put(engine, "Large", "weak", "", "orchid " + "irrelevant " * 80)
        _put(engine, "Small", "answer", "", "orchid")
        pk = {"Large": "id", "Small": "id"}
        large = _fts_seeds(engine, "Large", "orchid", 32, pk)
        small = _fts_seeds(engine, "Small", "orchid", 32, pk)
        assert (
            large[0].score > small[0].score
        )  # rarity in Large creates a misleading raw-score advantage
        response = search_knowledge(
            engine, engine.config, SearchRequest(query="orchid", top_k=1, hops=0)
        )
        assert response.seeds[0].node.id == "Small:answer"
    finally:
        engine.close()


def test_source_metadata_never_changes_lexical_ranking(ranked_engine):
    request = SearchRequest(query="orchid", top_k=2, hops=0)
    before = _signature(search_knowledge(ranked_engine, ranked_engine.config, request))
    ranked_engine.execute_write(
        "MATCH (n:Decision {id: 'orchid'}) SET n._source = $source",
        {"source": "unrelated " * 1000},
    )
    after = _signature(search_knowledge(ranked_engine, ranked_engine.config, request))
    assert before == after


def test_single_label_preserves_native_bm25_order(ranked_engine):
    hits = _fts_seeds(
        ranked_engine, "Function", "orchid backend", 32, {"Function": "id"}
    )
    assert [s.score for s in hits] == sorted((s.score for s in hits), reverse=True)
    response = search_knowledge(
        ranked_engine,
        ranked_engine.config,
        SearchRequest(query="orchid backend", labels=["Function"], top_k=6, hops=0),
    )
    assert [s.node.id for s in response.seeds] == [s.node.id for s in hits[:6]]


def _hit(key, score, match="fts", label="Memory"):
    return ScoredNode(
        node=NodeRecord(id=f"{label}:{key}", label=label, properties={"id": key}),
        score=score,
        match=match,
    )


def test_fusion_uses_scores_and_equal_ranks_not_row_order():
    fts = [_hit("a", 10), _hit("b", 10), _hit("c", 5)]
    vec = [_hit("b", 0.9, "vector"), _hit("a", 0.9, "vector"), _hit("d", 0.8, "vector")]
    reference = None
    for left, right in itertools.product(
        itertools.permutations(fts), itertools.permutations(vec)
    ):
        for channels in [
            {"fts": list(left), "vector": list(right)},
            {"vector": list(right), "fts": list(left)},
        ]:
            result = [(s.node.id, s.score, s.match) for s in _rrf_fuse(channels)]
            assert result == reference if reference is not None else True
            reference = result
    assert reference[:2] == [("Memory:a", 2 / 61, "fts"), ("Memory:b", 2 / 61, "fts")]
    assert [row[0] for row in reference[2:]] == ["Memory:c", "Memory:d"]


def test_duplicate_and_nonfinite_hits_cannot_inflate_fusion():
    clean = {"fts": [_hit("a", 10), _hit("b", 5)]}
    noisy = {
        "fts": [
            *clean["fts"],
            _hit("a", 10),
            _hit("a", 1),
            _hit("bad", math.nan),
            _hit("infinite", math.inf),
        ]
    }
    assert _rrf_fuse(clean) == _rrf_fuse(noisy)


@pytest.mark.parametrize("cap", [0, 1, 2, 10])
def test_diversity_has_no_duplicates_and_backfills_available_slots(cap):
    hits = [_hit(str(i), 10 - i, label="Code") for i in range(5)] + [
        _hit("decision", 1, label="Decision")
    ]
    chosen = _diversify(hits, 5, cap)
    assert len(chosen) == len({s.node.id for s in chosen}) == 5
    if cap in (1, 2):
        assert chosen[cap].node.label == "Decision"
        assert [s.node.id for s in chosen if s.node.label == "Code"] == [
            f"Code:{i}" for i in range(4)
        ]
    else:
        assert all(s.node.label == "Code" for s in chosen)
    assert len(_diversify(hits[:3], 8, cap)) == 3


@pytest.mark.parametrize("codec", ["fp32", "int8", "binary", "polar"])
def test_hybrid_label_permutations_and_duplicate_filters(
    ranked_engine, monkeypatch, codec
):
    fake = FakeEmbedder(synonyms={"automobile": "car"})
    monkeypatch.setattr(vectors, "get_embedder", lambda _: fake)
    cfg = ranked_engine.config.model_copy(
        update={
            "embedder": EmbedderConfig(
                provider="fastembed", model="fake", dim=FAKE_DIM
            ),
            "vector_codec": codec,
        }
    )
    _put(ranked_engine, "Task", "car", "car", "car repair maintenance")
    # Warm all tables once; each permutation must observe the same indexed state.
    search_knowledge(
        ranked_engine, cfg, SearchRequest(query="automobile backend", top_k=6, hops=0)
    )
    reference = None
    for labels in [
        list(p) for p in itertools.permutations(["Function", "Decision", "Task"])
    ] + [["Task", "Task", "Ghost", "Decision", "Function"]]:
        response = search_knowledge(
            ranked_engine,
            cfg,
            SearchRequest(query="automobile backend", labels=labels, top_k=6, hops=0),
        )
        assert response.vector_status is None
        assert response.pending_embeddings == 0
        assert "Task:car" in {s.node.id for s in response.seeds}
        signature = _signature(response)
        assert signature == reference if reference is not None else True
        reference = signature


def test_vector_only_diversity_sees_labels_below_global_shortlist(
    tmp_path, monkeypatch
):
    class TwoDirections:
        dim, model_id = 4, "two-directions"

        def embed(self, texts):
            return [
                [1.0, 0.5, 0.0, 0.0] if "reference" in text else [1.0, 0.0, 0.0, 0.0]
                for text in texts
            ]

    monkeypatch.setattr(vectors, "get_embedder", lambda _: TwoDirections())
    engine = Engine(
        GragConfig(
            db_path=tmp_path / "vector-flood.lbdb",
            search_label_cap=1,
            embedder=EmbedderConfig(provider="fastembed", model="fake", dim=4),
        )
    )
    try:
        for label in ["Code", "Decision"]:
            _table(engine, label)
        for i in range(40):
            _put(engine, "Code", f"f-{i:02d}", "car", "car helper")
        _put(engine, "Decision", "reference", "reference", "car ownership reference")
        request = SearchRequest(query="automobile", top_k=3, hops=0)
        response = search_knowledge(engine, engine.config, request)
        assert response.vector_status is None
        assert [(s.node.label, s.match) for s in response.seeds] == [
            ("Code", "vector"),
            ("Decision", "vector"),
            ("Code", "vector"),
        ]
        uncapped = search_knowledge(
            engine, engine.config.model_copy(update={"search_label_cap": 0}), request
        )
        assert all(s.node.label == "Code" for s in uncapped.seeds)
        assert (
            len(vectors.vector_candidates(engine, engine.config, "automobile", None, 3))
            == 3
        )
    finally:
        engine.close()


def test_vector_failure_retains_order_independent_lexical_ranking(
    ranked_engine, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("offline model")

    monkeypatch.setattr("grag.retrieval.search.vector_candidates", fail)
    cfg = ranked_engine.config.model_copy(
        update={"embedder": EmbedderConfig(provider="fastembed", dim=FAKE_DIM)}
    )
    for labels in [["Function", "Decision"], ["Decision", "Function"]]:
        response = search_knowledge(
            ranked_engine,
            cfg,
            SearchRequest(query="orchid", labels=labels, top_k=1, hops=0),
        )
        assert response.seeds[0].node.id == "Decision:orchid"
        assert response.vector_status == "error"


def test_duplicate_label_filter_does_not_inflate_pending_counts(
    ranked_engine, monkeypatch
):
    monkeypatch.setattr(vectors, "get_embedder", lambda _: FakeEmbedder())
    cfg = ranked_engine.config.model_copy(
        update={
            "embedder": EmbedderConfig(
                provider="fastembed", model="fake", dim=FAKE_DIM
            ),
            "max_embed_per_search": 0,
        }
    )
    expected = ranked_engine.execute("MATCH (n:Decision) RETURN count(n)").rows[0][0]
    for labels in [["Decision"], ["Decision", "Decision", "Ghost"]]:
        response = search_knowledge(
            ranked_engine,
            cfg,
            SearchRequest(query="orchid", labels=labels, top_k=1, hops=0),
        )
        assert response.pending_embeddings == expected
        assert response.vector_status is None


def test_cross_label_search_remains_stable_after_reopen(ranked_engine):
    cfg = ranked_engine.config
    request = SearchRequest(query="why choose Python backend", top_k=5, hops=0)
    expected = _signature(search_knowledge(ranked_engine, cfg, request))
    ranked_engine.close()
    reopened = Engine(cfg)
    try:
        assert _signature(search_knowledge(reopened, cfg, request)) == expected
    finally:
        reopened.close()


@pytest.mark.parametrize("creation_order", [["Alpha", "Beta"], ["Beta", "Alpha"]])
def test_equal_lexical_scores_do_not_inherit_schema_creation_order(
    tmp_path, creation_order
):
    engine = Engine(GragConfig(db_path=tmp_path / "ties.lbdb"))
    try:
        for label in creation_order:
            _table(engine, label)
            _put(engine, label, "same", "", "orchid")
        response = search_knowledge(
            engine, engine.config, SearchRequest(query="orchid", top_k=2, hops=0)
        )
        assert [(s.node.id, s.score) for s in response.seeds] == [
            ("Alpha:same", 1 / 61),
            ("Beta:same", 1 / 61),
        ]
    finally:
        engine.close()


def test_large_vocabulary_is_stemmed_in_distinct_bounded_batches(tmp_path, monkeypatch):
    engine = Engine(GragConfig(db_path=tmp_path / "terms.lbdb"))
    try:
        for label in ["Alpha", "Beta"]:
            _table(engine, label)
        _put(
            engine,
            "Alpha",
            "weak",
            "",
            "orchid " + " ".join(f"word{i}" for i in range(1100)),
        )
        _put(engine, "Beta", "strong", "", "orchids")
        original = engine.execute
        batches = []

        def capture(query, params=None):
            if query.startswith("UNWIND $terms"):
                batches.append(params["terms"])
            return original(query, params)

        monkeypatch.setattr(engine, "execute", capture)
        response = search_knowledge(
            engine, engine.config, SearchRequest(query="orchid", top_k=1, hops=0)
        )
        assert response.seeds[0].node.id == "Beta:strong"
        assert len(batches) == 2 and all(len(batch) <= 1024 for batch in batches)
        assert not set(batches[0]) & set(batches[1])
    finally:
        engine.close()
