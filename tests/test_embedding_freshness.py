"""Embedding publication must match the input and configuration actually inferred."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core import mutate
from grag.core.errors import ConfigurationError
from grag.core.types import (
    EMB_FINGERPRINT_PROP,
    VECTOR_PROPS,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval import vectors
from grag.service import GragService


class TextEmbedder:
    dim = 2
    model_id = "fixture"

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


class BlockedEmbedder(TextEmbedder):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.inputs = []

    def embed(self, texts):
        self.inputs.extend(texts)
        self.started.set()
        assert self.release.wait(10), "test did not release inference"
        return super().embed(texts)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "get_embedder", lambda config: TextEmbedder())
    service = GragService(GragConfig(
        db_path=tmp_path / "fresh.lbdb", buffer_pool_size=128 * 1024 * 1024,
        embedder=EmbedderConfig(
            provider="fastembed", model="fixture", dim=2, text_props={"Memory": ["text"]},
        ),
    ))
    service.define_schema(DefineSchemaRequest(node_tables=[NodeTableSpec(
        name="Memory", properties=[PropertySpec(name="text"), PropertySpec(name="extra")],
    )]))
    put(service, "old")
    yield service
    service.close()


def put(svc, text, key="a"):
    svc.upsert_nodes(UpsertNodesRequest(nodes=[UpsertNode(
        label="Memory", key=key, properties={"text": text},
    )]))


def embed(svc, **kw):
    return vectors.embed_pending_nodes(svc.engine, svc.config, "Memory", **kw)


def pending(svc):
    return vectors.pending_embedding_count(svc.engine, svc.config, "Memory")


def row(svc):
    return svc.engine.execute("MATCH (n:Memory {id: 'a'}) RETURN n").rows[0][0]


@pytest.mark.parametrize("change", ["text", "null_to_empty", "delete", "replace"])
def test_intervening_writes_discard_inference(svc, monkeypatch, change):
    if change == "null_to_empty":
        svc.engine.execute_write("MATCH (n:Memory) SET n.text = NULL")
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(embed, svc, max_nodes=1)
        try:
            assert blocked.started.wait(5)
            if change in {"delete", "replace"}:
                pool.submit(svc.engine.execute_write, "MATCH (n:Memory) DETACH DELETE n").result(5)
            if change != "delete":
                pool.submit(put, svc, "" if change == "null_to_empty" else "updated text").result(5)
        finally:
            blocked.release.set()
        assert future.result(5) == 0
    if change == "delete":
        assert pending(svc) == 0
        assert svc.engine.execute("MATCH (n:Memory) RETURN n").rows == []
        return
    assert pending(svc) == 1
    assert {p: row(svc)[p] for p in VECTOR_PROPS} == dict.fromkeys(VECTOR_PROPS)
    monkeypatch.setattr(vectors, "get_embedder", lambda config: TextEmbedder())
    assert embed(svc) == 1
    text = row(svc)["text"] or ""  # empty STRING is an input, not PK fallback
    assert row(svc)["embedding"] == [float(len(text)), 1.0]
    assert pending(svc) == 0


def test_unchanged_input_can_commit_during_idempotent_upsert(svc, monkeypatch):
    class SameText(TextEmbedder):
        def embed(self, texts):
            put(svc, "old")
            return super().embed(texts)

    monkeypatch.setattr(vectors, "get_embedder", lambda config: SameText())
    assert embed(svc) == 1
    assert pending(svc) == 0


def test_newer_embedding_is_not_overwritten(svc, monkeypatch):
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(embed, svc)
        try:
            assert blocked.started.wait(5)
            put(svc, "a newer and longer memory")
            monkeypatch.setattr(vectors, "get_embedder", lambda config: TextEmbedder())
            assert embed(svc) == 1
            expected = row(svc)["embedding"]
        finally:
            blocked.release.set()
        assert old.result(5) == 0
    assert row(svc)["embedding"] == expected
    assert pending(svc) == 0


@pytest.mark.parametrize("setting,value", [
    ("model", "another-model"), ("provider", "remote"),
    ("base_url", "http://embedding.example/v1"), ("dim", 3),
    ("api_key_env", "OTHER_EMBEDDING_KEY"),
    ("document_prefix", "passage: "), ("query_prefix", "query: "),
    ("text_props", {"Memory": ["extra", "text"]}),
    ("vector_codec", "int8"), ("disabled", None),
    ("schema", None), ("polar_bits", "2.0"),
])
def test_configuration_change_during_inference_stays_pending(svc, monkeypatch, setting, value):
    if setting == "polar_bits":
        svc.config.vector_codec = "polar"
        monkeypatch.setenv("GRAG_POLAR_BITS_PER_DIM", "1.0")
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(embed, svc)
        try:
            assert blocked.started.wait(5)
            if setting == "disabled":
                svc.config.embedder = None
            elif setting == "vector_codec":
                svc.config.vector_codec = value
            elif setting == "schema":
                # Switching from explicit to default policy also tests new
                # prose columns entering the fingerprint during inference.
                svc.config.embedder.text_props = {}
                svc.engine.execute_write("ALTER TABLE Memory ADD body STRING")
            elif setting == "polar_bits":
                monkeypatch.setenv("GRAG_POLAR_BITS_PER_DIM", value)
            else:
                setattr(svc.config.embedder, setting, value)
        finally:
            blocked.release.set()
        assert old.result(5) == 0
    assert row(svc)["embedding"] is None
    assert pending(svc) == (0 if setting == "disabled" else 1)


@pytest.mark.parametrize("operation", ["switch", "switch_back", "reindex"])
def test_new_configuration_generation_fences_old_worker(svc, monkeypatch, operation):
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(embed, svc)
        try:
            assert blocked.started.wait(5)
            monkeypatch.setattr(vectors, "get_embedder", lambda config: TextEmbedder())
            if operation == "reindex":
                assert vectors.reindex_embeddings(svc.engine, svc.config, "Memory") == 1
            else:
                other = svc.config.model_copy(deep=True)
                other.embedder.model = "another-model"
                assert vectors.embed_pending_nodes(svc.engine, other, "Memory") == 1
                if operation == "switch_back":
                    # Prepare A again but leave it pending. A fingerprint
                    # alone cannot distinguish this from the old A worker.
                    svc.config.max_embed_per_search = 0
                    vectors.vector_candidates(svc.engine, svc.config, "query", ["Memory"], 1)
        finally:
            blocked.release.set()
        assert old.result(5) == 0
    if operation == "switch_back":
        assert row(svc)["embedding"] is None


def test_rejected_batch_is_bounded_and_counts_only_commits(svc, monkeypatch):
    put(svc, "other", key="b")
    calls = []

    class Conflict(TextEmbedder):
        def embed(self, texts):
            calls.append(texts)
            put(svc, "changed")
            return super().embed(texts)

    monkeypatch.setattr(vectors, "get_embedder", lambda config: Conflict())
    assert embed(svc, batch_size=2) == 1  # no retry loop on a conflicting row
    assert len(calls) == 1
    assert pending(svc) == 1


@pytest.mark.parametrize("codec", ["fp32", "int8", "binary", "polar"])
def test_configuration_change_rebuilds_lazily_without_serving_old_vectors(svc, codec):
    put(svc, "second", key="b")
    svc.config.vector_codec = codec
    assert embed(svc) == 2
    original = row(svc)[EMB_FINGERPRINT_PROP]
    # Search before invalidating to exercise the complete retrieval lifecycle.
    assert vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 2)
    svc.config.embedder.model = "another-model"
    assert pending(svc) == 2
    svc.config.max_embed_per_search = 0
    assert vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 2) == []
    assert pending(svc) == 2
    svc.config.max_embed_per_search = 1
    hits = vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 2)
    assert len(hits) == 1
    assert pending(svc) == 1
    assert embed(svc) == 1
    assert row(svc)[EMB_FINGERPRINT_PROP] != original


def test_codec_change_never_decodes_previous_codec(svc):
    svc.config.vector_codec = "int8"
    assert embed(svc) == 1
    svc.config.vector_codec = "binary"
    assert pending(svc) == 1
    assert vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 1)
    assert len(row(svc)["_emb_code"]) == 1
    assert pending(svc) == 0


def test_legacy_vectors_without_fingerprint_are_pending(svc):
    svc.engine.execute_write("ALTER TABLE Memory ADD embedding FLOAT[2]")
    svc.engine.execute_write("MATCH (n:Memory) SET n.embedding = [99.0, 0.0]")
    assert pending(svc) == 1
    assert embed(svc) == 1
    assert row(svc)["embedding"] == [3.0, 1.0]
    assert row(svc)[EMB_FINGERPRINT_PROP]


def test_fingerprint_survives_restart_and_policy_change(svc):
    assert embed(svc) == 1
    fingerprint = row(svc)[EMB_FINGERPRINT_PROP]
    svc.close()
    reopened = GragService(svc.config)
    try:
        assert pending(reopened) == 0
        assert embed(reopened) == 0
        assert row(reopened)[EMB_FINGERPRINT_PROP] == fingerprint
        reopened.config.embedder.text_props = {}
        reopened.config.embedder.exclude_props = ["id", "extra"]
        assert pending(reopened) == 0  # same effective text policy
        reopened.config.embedder.exclude_props = ["id"]
        assert pending(reopened) == 1
        assert embed(reopened) == 1
    finally:
        reopened.close()


def test_dimension_change_preserves_data_and_reports_configuration_error(svc):
    assert embed(svc) == 1
    previous = row(svc)["embedding"]
    svc.config.embedder.dim = 3
    with pytest.raises(ConfigurationError, match="dim=3"):
        embed(svc)
    assert row(svc)["embedding"] == previous
    assert pending(svc) == 1


def test_query_configuration_change_discards_results(svc, monkeypatch):
    assert embed(svc) == 1

    class ChangeOnQuery(TextEmbedder):
        def embed(self, texts):
            svc.config.embedder.model = "different"
            return super().embed(texts)

    monkeypatch.setattr(vectors, "get_embedder", lambda config: ChangeOnQuery())
    assert vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 1) == []
    assert pending(svc) == 1


def test_upsert_check_and_invalidation_exclude_competing_writers(svc, monkeypatch):
    assert embed(svc) == 1
    checked = threading.Event()
    release = threading.Event()
    competing = threading.Event()
    original = mutate._searchable_text_changed

    def pause_after_read(*args, **kw):
        changed = original(*args, **kw)
        if kw["accepted"].get("text") == "old":
            checked.set()
            assert release.wait(5)
        return changed

    def other_write():
        competing.set()
        put(svc, "new text")

    monkeypatch.setattr(mutate, "_searchable_text_changed", pause_after_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(put, svc, "old")
        try:
            assert checked.wait(5)
            second = pool.submit(other_write)
            assert competing.wait(5)
            with pytest.raises(TimeoutError):
                second.result(0.1)
        finally:
            release.set()
        first.result(5)
        second.result(5)
    assert row(svc)["text"] == "new text"
    assert row(svc)["embedding"] is None
    assert pending(svc) == 1


def test_background_worker_retries_intervening_edit(svc, monkeypatch):
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    assert svc.start_background_embedding()
    worker = svc.embed_worker
    try:
        assert blocked.started.wait(5)
        put(svc, "new background memory")  # wakes another pass while inference runs
        monkeypatch.setattr(vectors, "get_embedder", lambda config: TextEmbedder())
    finally:
        blocked.release.set()
    assert worker.wait_idle(10)
    assert pending(svc) == 0
    assert worker.embedded_total == 1  # discarded attempt is not reported as progress
    assert row(svc)["embedding"] == [float(len("new background memory")), 1.0]
    assert worker.last_error is None


def test_first_vector_ddl_excludes_concurrent_upsert(svc, monkeypatch):
    checked = threading.Event()
    release = threading.Event()
    original = mutate._searchable_text_changed

    def pause_before_update(*args, **kw):
        changed = original(*args, **kw)
        checked.set()
        assert release.wait(5)
        return changed

    monkeypatch.setattr(mutate, "_searchable_text_changed", pause_before_update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(put, svc, "newly embedded text")
        try:
            assert checked.wait(5)
            inference = pool.submit(embed, svc)
            with pytest.raises(TimeoutError):
                inference.result(0.1)
        finally:
            release.set()
        update.result(5)
        assert inference.result(5) == 1
    assert row(svc)["embedding"] == [float(len("newly embedded text")), 1.0]


@pytest.mark.parametrize("codec", ["fp32", "int8"])
def test_shortlist_edit_does_not_return_a_score_for_old_text(svc, monkeypatch, codec):
    svc.config.vector_codec = codec
    assert embed(svc) == 1
    original = vectors._fetch_nodes_by_keys

    def edit_before_fetch(*args, **kw):
        put(svc, "edited during ranking")
        return original(*args, **kw)

    monkeypatch.setattr(vectors, "_fetch_nodes_by_keys", edit_before_fetch)
    assert vectors.vector_candidates(svc.engine, svc.config, "old", ["Memory"], 1) == []
    assert pending(svc) == 1


def test_new_nodes_after_lazy_vector_ddl_are_pending(svc):
    assert embed(svc) == 1
    put(svc, "new", key="b")
    assert pending(svc) == 1
    assert embed(svc) == 1
    assert pending(svc) == 0


def test_schema_addition_changes_default_embedding_policy(svc, monkeypatch):
    svc.config.embedder.text_props = {}
    blocked = BlockedEmbedder()
    monkeypatch.setattr(vectors, "get_embedder", lambda config: blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(embed, svc)
        try:
            assert blocked.started.wait(5)
            svc.engine.execute_write("ALTER TABLE Memory ADD body STRING")
            svc.upsert_nodes(UpsertNodesRequest(nodes=[UpsertNode(
                label="Memory", key="a", properties={"body": "new prose"},
            )]))
        finally:
            blocked.release.set()
        assert old.result(5) == 0
    assert pending(svc) == 1
