"""Vector pipeline tests. No real embedding models: FakeEmbedder derives
deterministic vectors from token hashes, so texts sharing tokens score
higher — enough to exercise the full vector path without fastembed/torch.
"""

from __future__ import annotations

import hashlib
import importlib.util

import numpy as np
import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError, GragError
from grag.core.types import (
    EMB_CODE_PROP,
    EMB_MODEL_PROP,
    EMB_MAGNITUDE_PROP,
    EMBEDDING_PROP,
)
from grag.retrieval import vectors

FAKE_DIM = 32


class FakeEmbedder:
    """Deterministic hash embedder: each whitespace token signs a few dims."""

    def __init__(self, dim: int = FAKE_DIM, synonyms: dict[str, str] | None = None):
        self.dim = dim
        self.model_id = f"fake-hash-{dim}"
        self.synonyms = synonyms or {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for tok in text.lower().split():
                tok = self.synonyms.get(tok, tok)
                d = hashlib.sha256(tok.encode()).digest()
                for j in range(3):
                    idx = d[j] % self.dim
                    sign = 1.0 if d[16 + j] % 2 == 0 else -1.0
                    v[idx] += sign
            out.append([float(x) for x in v])
        return out


DOCS = [
    ("doc-0", "graph databases", "graph databases store relationships between entities"),
    ("doc-1", "vector search", "vector embeddings enable semantic retrieval"),
    ("doc-2", "context packing", "token budgets matter for llm prompts"),
]


def make_docs(eng: Engine) -> Engine:
    eng.execute_write(
        "CREATE NODE TABLE Doc(id STRING PRIMARY KEY, title STRING, text STRING)"
    )
    eng.execute_write("CREATE REL TABLE RELATED(FROM Doc TO Doc, since INT64)")
    for did, title, text in DOCS:
        eng.execute_write(
            "CREATE (d:Doc {id: $id, title: $t, text: $x})",
            {"id": did, "t": title, "x": text},
        )
    eng.execute_write(
        "MATCH (a:Doc {id: 'doc-0'}), (b:Doc {id: 'doc-1'}) "
        "CREATE (a)-[:RELATED {since: 2024}]->(b)"
    )
    eng.execute_write(
        "MATCH (a:Doc {id: 'doc-1'}), (b:Doc {id: 'doc-2'}) "
        "CREATE (a)-[:RELATED {since: 2025}]->(b)"
    )
    return eng


@pytest.fixture()
def fake() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture()
def vconfig(tmp_path) -> GragConfig:
    return GragConfig(
        db_path=tmp_path / "vec.lbdb",
        embedder=EmbedderConfig(provider="fastembed", model="fake", dim=FAKE_DIM),
    )


@pytest.fixture()
def vengine(vconfig, monkeypatch, fake):
    monkeypatch.setattr(
        vectors,
        "get_embedder",
        lambda config: fake if config.embedder is not None else None,
    )
    eng = Engine(vconfig)
    make_docs(eng)
    yield eng
    eng.close()


# --- embedders ---------------------------------------------------------------


def test_get_embedder_none_without_config(tmp_path):
    assert vectors.get_embedder(GragConfig(db_path=tmp_path / "x.lbdb")) is None


def test_fastembed_missing_raises_with_hint(tmp_path):
    if importlib.util.find_spec("fastembed") is not None:
        pytest.skip("fastembed installed in this environment")
    vectors._EMBEDDER_CACHE.clear()
    cfg = GragConfig(
        db_path=tmp_path / "x.lbdb",
        embedder=EmbedderConfig(provider="fastembed", dim=FAKE_DIM),
    )
    with pytest.raises(ConfigurationError) as exc_info:
        vectors.get_embedder(cfg)
    assert "gragdb[embed-local]" in str(exc_info.value)


def test_remote_embedder_payload_and_key(tmp_path, monkeypatch):
    import httpx

    calls: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            n = len(calls["json"]["input"])
            return {"data": [{"index": i, "embedding": [1.0, 2.0]} for i in range(n)]}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.update(url=url, json=json, headers=headers)
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("GRAG_TEST_EMBED_KEY", "sekret")
    vectors._EMBEDDER_CACHE.clear()
    cfg = GragConfig(
        db_path=tmp_path / "x.lbdb",
        embedder=EmbedderConfig(
            provider="remote",
            model="m1",
            dim=2,
            base_url="http://embed.local/v1/",
            api_key_env="GRAG_TEST_EMBED_KEY",
        ),
    )
    emb = vectors.get_embedder(cfg)
    out = emb.embed(["a", "b"])
    assert calls["url"] == "http://embed.local/v1/embeddings"
    assert calls["json"] == {"model": "m1", "input": ["a", "b"]}
    assert calls["headers"]["Authorization"] == "Bearer sekret"
    assert out == [[1.0, 2.0], [1.0, 2.0]]


def test_embedder_cached_per_process(vconfig, fake, monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(vectors, "_EMBEDDER_CACHE", cache)
    cache[("fastembed", "fake", FAKE_DIM, None, None)] = fake
    assert vectors.get_embedder(vconfig) is fake


# --- polar split + codecs ------------------------------------------------------


def test_split_magnitude_unit_norm():
    r, u = vectors.split_magnitude(np.array([3.0, 4.0]))
    assert r == pytest.approx(5.0)
    assert float(np.linalg.norm(u)) == pytest.approx(1.0)
    assert u.tolist() == pytest.approx([0.6, 0.8])


def test_split_magnitude_zero_vector():
    r, u = vectors.split_magnitude(np.zeros(8))
    assert r == 0.0
    assert np.all(u == 0)


def test_int8_roundtrip_close_to_original():
    rng = np.random.default_rng(7)
    u = rng.normal(size=64).astype(np.float32)
    u /= np.linalg.norm(u)
    blob = vectors.encode_direction(u, "int8")
    assert len(blob) == 4 + 64
    back = vectors.decode_direction(blob, "int8", 64)
    cos = float(back @ u / (np.linalg.norm(back) * np.linalg.norm(u)))
    assert cos > 0.999
    assert float(np.max(np.abs(back - u))) < 0.01


def test_binary_roundtrip_sign_agreement():
    rng = np.random.default_rng(11)
    u = rng.normal(size=64).astype(np.float32)  # exact zeros ~impossible
    blob = vectors.encode_direction(u, "binary")
    assert len(blob) == 8  # 64 sign bits
    back = vectors.decode_direction(blob, "binary", 64)
    assert np.all(np.sign(back) == np.sign(u))
    assert float(np.linalg.norm(back)) == pytest.approx(1.0)


def test_fp32_roundtrip_exact():
    u = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    back = vectors.decode_direction(vectors.encode_direction(u, "fp32"), "fp32", 3)
    assert np.allclose(back, u)


def test_polar_codec_rejected_for_now():
    with pytest.raises(ConfigurationError):
        vectors.encode_direction(np.ones(4), "polar")
    with pytest.raises(ConfigurationError):
        vectors.decode_direction(b"\x00", "polar", 1)
    with pytest.raises(ConfigurationError):
        vectors.candidate_scores([b"\x00"], "polar", np.ones(1))


def test_candidate_scores_ordering():
    rng = np.random.default_rng(3)
    base = rng.normal(size=48).astype(np.float32)
    base /= np.linalg.norm(base)
    near = base + 0.05 * rng.normal(size=48).astype(np.float32)
    near /= np.linalg.norm(near)
    far = -base
    for codec in ("fp32", "int8", "binary"):
        codes = [vectors.encode_direction(v, codec) for v in (base, near, far)]
        scores = vectors.candidate_scores(codes, codec, base)
        assert scores.shape == (3,)
        assert scores[0] > scores[1] > scores[2]


def test_candidate_scores_empty():
    out = vectors.candidate_scores([], "int8", np.ones(8, dtype=np.float32))
    assert out.shape == (0,)


# --- storage + embedding writes -----------------------------------------------


def test_ensure_vector_storage_idempotent(vengine, vconfig):
    vectors.ensure_vector_storage(vengine, vconfig, "Doc")
    first = vectors.table_properties(vengine, "Doc")
    for col in (EMBEDDING_PROP, EMB_MAGNITUDE_PROP, EMB_CODE_PROP, EMB_MODEL_PROP):
        assert col in first
    assert first[EMBEDDING_PROP] == f"FLOAT[{FAKE_DIM}]"
    assert first[EMB_CODE_PROP] in ("UINT8[]", "BLOB")
    vectors.ensure_vector_storage(vengine, vconfig, "Doc")  # second call: no error
    assert vectors.table_properties(vengine, "Doc") == first


def test_ensure_vector_storage_noop_without_embedder(engine):
    vectors.ensure_vector_storage(engine, engine.config, "Anything")


@pytest.mark.parametrize("codec", ["fp32", "int8", "binary"])
def test_embed_pending_nodes_fills_columns(vengine, vconfig, codec):
    vconfig.vector_codec = codec
    n = vectors.embed_pending_nodes(vengine, vconfig, "Doc")
    assert n == 3
    res = vengine.execute(
        f"MATCH (d:Doc) WHERE d.{EMBEDDING_PROP} IS NOT NULL "
        f"RETURN d.id, d.{EMB_MAGNITUDE_PROP}, d.{EMB_CODE_PROP}, d.{EMB_MODEL_PROP}, "
        f"d.{EMBEDDING_PROP}"
    )
    assert len(res.rows) == 3
    expected_code_len = {"fp32": 4 * FAKE_DIM, "int8": 4 + FAKE_DIM, "binary": FAKE_DIM // 8}
    for _id, r, code, model, emb in res.rows:
        assert r > 0
        assert model == f"fake-hash-{FAKE_DIM}"
        assert code is not None and len(code) == expected_code_len[codec]
        assert len(emb) == FAKE_DIM
    assert vectors.embed_pending_nodes(vengine, vconfig, "Doc") == 0


def test_embed_pending_nodes_batching(vengine, vconfig):
    n = vectors.embed_pending_nodes(vengine, vconfig, "Doc", batch_size=2)
    assert n == 3


def test_embed_pending_nodes_no_embedder(engine):
    assert vectors.embed_pending_nodes(engine, engine.config, "Doc") == 0


# --- vector candidates ----------------------------------------------------------


@pytest.mark.parametrize("codec", ["fp32", "int8", "binary"])
def test_vector_candidates_finds_semantic_doc(vengine, vconfig, codec):
    vconfig.vector_codec = codec
    hits = vectors.vector_candidates(vengine, vconfig, "semantic vector retrieval", None, 2)
    assert hits
    assert hits[0].node.id == "Doc:doc-1"
    assert all(h.match == "vector" for h in hits)
    assert hits[0].score >= hits[-1].score
    assert -1.0 <= hits[0].score <= 1.0


def test_vector_candidates_no_embedder_returns_empty(engine):
    assert vectors.vector_candidates(engine, engine.config, "anything", None, 4) == []


def test_vector_candidates_labels_filter(vengine, vconfig):
    hits = vectors.vector_candidates(vengine, vconfig, "semantic vector retrieval", ["Doc", "Nope"], 3)
    assert hits and hits[0].node.id == "Doc:doc-1"
    assert vectors.vector_candidates(vengine, vconfig, "semantic vector retrieval", ["Nope"], 3) == []


def test_vector_candidates_exact_scan_fallback(vengine, vconfig, monkeypatch):
    vconfig.vector_codec = "fp32"

    def boom(*args, **kwargs):
        raise GragError("vector index unavailable in this build")

    monkeypatch.setattr(vectors, "_query_vector_index", boom)
    hits = vectors.vector_candidates(vengine, vconfig, "semantic vector retrieval", None, 2)
    assert hits and hits[0].node.id == "Doc:doc-1"


def test_vector_candidates_second_call_reuses_index(vengine, vconfig):
    vconfig.vector_codec = "fp32"
    first = vectors.vector_candidates(vengine, vconfig, "graph relationships", None, 2)
    second = vectors.vector_candidates(vengine, vconfig, "graph relationships", None, 2)
    assert [h.node.id for h in first] == [h.node.id for h in second]
    assert first[0].node.id == "Doc:doc-0"
