"""Embedding text selection and model-family retrieval prefixes."""

from __future__ import annotations

from grag.config import DEFAULT_EMBED_EXCLUDE_PROPS, EmbedderConfig, GragConfig
from grag.core.types import IngestDocument, IngestRequest, SearchRequest
from grag.retrieval import vectors
from grag.retrieval.vectors import embed_text_props, model_prefixes, resolve_prefixes
from grag.service import GragService
from test_vectors import FAKE_DIM, FakeEmbedder

PROPS = {
    "id": "STRING",
    "text": "STRING",
    "meta": "STRING",
    "path": "STRING",
    "line_start": "INT64",
    "_source": "STRING",
    "embedding": "FLOAT[32]",
}


def _cfg(**kw) -> EmbedderConfig:
    kw.setdefault("model", "fake")
    return EmbedderConfig(provider="fastembed", dim=FAKE_DIM, **kw)


def test_default_exclusions_drop_side_car_props():
    assert embed_text_props(_cfg(), "Chunk", PROPS) == ["id", "text"]
    assert "meta" in DEFAULT_EMBED_EXCLUDE_PROPS and "path" in DEFAULT_EMBED_EXCLUDE_PROPS


def test_explicit_text_props_win_and_ignore_unknown_names():
    cfg = _cfg(text_props={"Function": ["docstring", "text", "nope"]})
    assert embed_text_props(cfg, "Function", PROPS) == ["text"]
    # other labels keep the default policy
    assert embed_text_props(cfg, "Chunk", PROPS) == ["id", "text"]


def test_exclusion_never_leaves_a_table_without_text():
    cfg = _cfg(exclude_props=["id", "text", "meta", "path"])
    assert embed_text_props(cfg, "Chunk", PROPS) == ["id", "text", "meta", "path"]


def test_model_family_prefixes():
    assert model_prefixes("BAAI/bge-small-en-v1.5")[0].startswith("Represent this sentence")
    assert model_prefixes("BAAI/bge-small-en-v1.5")[1] == ""
    assert model_prefixes("nomic-ai/nomic-embed-text-v1.5") == (
        "search_query: ",
        "search_document: ",
    )
    assert model_prefixes("intfloat/e5-base-v2") == ("query: ", "passage: ")
    assert model_prefixes("BAAI/bge-m3") == ("", "")
    assert model_prefixes("jinaai/jina-embeddings-v2-base-code") == ("", "")


def test_configured_prefixes_override_family_defaults():
    cfg = _cfg(model="nomic-ai/nomic-embed-text-v1.5", query_prefix="", document_prefix="D: ")
    assert resolve_prefixes(cfg) == ("", "D: ")
    cfg = _cfg(model="nomic-ai/nomic-embed-text-v1.5")
    assert resolve_prefixes(cfg) == ("search_query: ", "search_document: ")


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("GRAG_EMBED_PROVIDER", "fastembed")
    monkeypatch.setenv("GRAG_EMBED_QUERY_PREFIX", "q> ")
    monkeypatch.setenv("GRAG_EMBED_EXCLUDE_PROPS", "meta, path ,")
    cfg = GragConfig.from_env().embedder
    assert cfg is not None
    assert cfg.query_prefix == "q> " and cfg.document_prefix is None
    assert cfg.exclude_props == ["meta", "path"]


def test_prefixes_and_exclusions_reach_the_embedder(tmp_path, monkeypatch):
    seen: list[str] = []

    class Spy(FakeEmbedder):
        def embed(self, texts):
            seen.extend(texts)
            return super().embed(texts)

    spy = Spy()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: spy)
    svc = GragService(
        GragConfig(
            db_path=tmp_path / "prefix.lbdb",
            embedder=EmbedderConfig(
                provider="fastembed", model="nomic-ai/nomic-embed-text-v1.5", dim=FAKE_DIM
            ),
        )
    )
    try:
        svc.ingest(
            IngestRequest(
                documents=[
                    IngestDocument(
                        text="graph databases store relationships",
                        source="a.md",
                        metadata={"secret": "zzz-meta-json"},
                    )
                ],
                chunk=False,
            )
        )
        docs = [t for t in seen if t.startswith("search_document: ")]
        assert docs and all("zzz-meta-json" not in t for t in docs)  # meta excluded
        seen.clear()
        svc.search_knowledge(SearchRequest(query="relationships", hops=0))
        assert seen == ["search_query: relationships"]
    finally:
        svc.close()
