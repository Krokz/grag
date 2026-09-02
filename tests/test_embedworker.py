"""Background embedding worker + background ingest jobs."""

from __future__ import annotations

import time

import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core.types import (
    EMBEDDING_PROP,
    IngestDocument,
    IngestRequest,
    SearchRequest,
)
from grag.embedworker import EmbedWorker, attached_worker
from grag.retrieval import vectors
from grag.service import GragService
from test_vectors import FAKE_DIM, FakeEmbedder


@pytest.fixture()
def hybrid_service(tmp_path, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(
        vectors,
        "get_embedder",
        lambda config: fake if config.embedder is not None else None,
    )
    svc = GragService(
        GragConfig(
            db_path=tmp_path / "bg.lbdb",
            embedder=EmbedderConfig(provider="fastembed", model="fake", dim=FAKE_DIM),
        )
    )
    yield svc
    svc.close()


def _pending(svc: GragService, label: str) -> int:
    return vectors.pending_embedding_count(svc.engine, svc.config, label)


def _ingest_three(svc: GragService) -> None:
    svc.ingest(
        IngestRequest(
            documents=[
                IngestDocument(text="graph databases store relationships", source="a.md"),
                IngestDocument(text="vector embeddings enable retrieval", source="b.md"),
                IngestDocument(text="token budgets matter for prompts", source="c.md"),
            ],
            chunk=False,
        )
    )


def test_without_worker_ingest_embeds_inline(hybrid_service):
    _ingest_three(hybrid_service)
    assert _pending(hybrid_service, "Chunk") == 0


def test_worker_drains_ingest_off_the_request_path(hybrid_service, monkeypatch):
    # Make inline embedding loud: with a worker attached it must never run
    # on the ingest path.
    real = vectors.embed_pending_nodes
    inline_calls = []

    def spy(engine, config, table, **kw):
        if kw.get("max_nodes") is None:
            inline_calls.append(table)  # ingest's unbounded drain signature
        return real(engine, config, table, **kw)

    monkeypatch.setattr(vectors, "embed_pending_nodes", spy)
    assert hybrid_service.start_background_embedding() is True
    worker = hybrid_service.embed_worker
    assert worker is not None and worker.running
    assert attached_worker(hybrid_service.engine) is worker

    _ingest_three(hybrid_service)
    assert inline_calls == []
    assert worker.wait_idle(timeout=20)
    assert _pending(hybrid_service, "Chunk") == 0
    assert worker.embedded_total == 3
    assert worker.status()["last_error"] is None


def test_search_never_embeds_inline_when_worker_attached(hybrid_service, monkeypatch):
    _ingest_three(hybrid_service)  # embedded inline (no worker yet)
    hybrid_service.engine.execute_write(f"MATCH (c:Chunk) SET c.{EMBEDDING_PROP} = NULL")
    assert _pending(hybrid_service, "Chunk") == 3

    hybrid_service.start_background_embedding()
    worker = hybrid_service.embed_worker
    # Freeze the worker so we can observe that the search itself embeds nothing.
    woke = []
    monkeypatch.setattr(worker, "drain_once", lambda: 0)
    original_wake = worker.wake
    monkeypatch.setattr(
        worker, "wake", lambda table=None: (woke.append(table), original_wake(table))
    )

    resp = hybrid_service.search_knowledge(SearchRequest(query="graph", hops=0))
    assert resp.pending_embeddings == 3  # nothing embedded on the request thread
    assert resp.seeds  # FTS still answers
    assert woke  # but the worker was nudged


def test_worker_survives_a_failing_pass(hybrid_service, monkeypatch):
    worker = EmbedWorker(hybrid_service.engine, hybrid_service.config, idle_poll_seconds=0.2)
    calls = {"n": 0}
    real = worker.drain_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("embedder hiccup")
        return real()

    monkeypatch.setattr(worker, "drain_once", flaky)
    worker.start()
    try:
        deadline = time.time() + 10
        while calls["n"] < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert calls["n"] >= 2
        assert worker.running
    finally:
        worker.stop()
    assert not worker.running


def test_service_close_stops_worker(hybrid_service):
    hybrid_service.start_background_embedding()
    worker = hybrid_service.embed_worker
    hybrid_service.close()
    assert not worker.running
    assert hybrid_service.embed_worker is None
