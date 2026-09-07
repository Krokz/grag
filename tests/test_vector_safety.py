"""Contain native regressions in child processes, including old HNSW files."""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError, CypherError
from grag.core.types import DefineSchemaRequest, SearchRequest, UpsertNodesRequest
from grag.retrieval import vectors
from grag.service import GragService


class FixtureEmbedder:
    model_id = "vector-safety-fixture"

    def embed(self, texts):
        result = []
        for text in texts:
            rng = random.Random(hashlib.sha256(text.encode()).hexdigest())  # noqa: S311 — reproducible vectors
            result.append([rng.uniform(-1, 1) for _ in range(32)])
        return result


def _config(path):
    return GragConfig(
        db_path=path, buffer_pool_size=128 * 1024**2, auto_refresh_code=False,
        embedder=EmbedderConfig(provider="fastembed", model="vector-safety-fixture", dim=32),
    )


def _put(svc, revision, *, count=256):
    result = svc.upsert_nodes(UpsertNodesRequest(nodes=[
        {"label": "Memory", "key": str(i), "source": "vector safety regression",
         "properties": {"text": None if revision is None else f"Memory {i} revision {revision}"}}
        for i in range(count)
    ]))
    assert not result.warnings


def _schema(svc):
    svc.define_schema(DefineSchemaRequest(node_tables=[
        {"name": "Memory", "properties": [{"name": "text"}]},
    ]))


def _search(svc):
    return svc.search_knowledge(SearchRequest(
        query="Memory 3 revision updated", labels=["Memory"], top_k=3, hops=0,
    ))


def _hnsw(engine):
    return [r for r in engine.execute("CALL SHOW_INDEXES() RETURN *").as_dicts()
            if r["index_type"] == "HNSW"]


def _worker(mode, path):
    vectors.get_embedder = lambda config: FixtureEmbedder()
    cfg = _config(path)
    svc = GragService(cfg)
    try:
        _schema(svc)
        _put(svc, "initial")
        assert vectors.embed_pending_nodes(svc.engine, cfg, "Memory") == 256
        _search(svc)
        if mode == "legacy":
            # Simulate the old release's persisted index, then use a fresh engine.
            if not _hnsw(svc.engine):
                svc.engine.execute_write(
                    "CALL CREATE_VECTOR_INDEX('Memory', 'grag_vec__Memory', "
                    "'embedding', metric := 'cosine')"
                )
            before = svc.engine.execute("MATCH (n:Memory) RETURN n ORDER BY n.id").rows
            svc.close()
            svc = GragService(cfg)
            assert svc.engine.execute("MATCH (n:Memory) RETURN n ORDER BY n.id").rows == before
        if mode == "null":
            _put(svc, None)
        elif mode == "config":
            cfg.embedder.model = "new-model"
            assert vectors.pending_embedding_count(svc.engine, cfg, "Memory") == 256
        elif mode == "delete":
            with svc.engine.write_transaction():
                svc.engine.execute_write("MATCH (n:Memory) DETACH DELETE n")
            _put(svc, "updated")
        elif mode == "rollback":
            try:
                with svc.engine.write_transaction():
                    _put(svc, "discarded")
                    svc.engine.execute_write("MATCH (n:Memory {id:'0'}) DETACH DELETE n")
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
            assert vectors.pending_embedding_count(svc.engine, cfg, "Memory") == 0
            assert svc.engine.execute("MATCH (n:Memory) RETURN count(n)").rows == [[256]]
            _put(svc, "updated")
        elif mode == "reindex":
            assert vectors.reindex_embeddings(svc.engine, cfg, "Memory") == 256
            _put(svc, "updated")
        else:
            _put(svc, "updated")
        print("Inputs changed; refilling vectors.", flush=True)
        assert vectors.embed_pending_nodes(svc.engine, cfg, "Memory") == 256
        assert vectors.pending_embedding_count(svc.engine, cfg, "Memory") == 0
        _search(svc)
        assert not _hnsw(svc.engine), "grag must not recreate the unsafe native index"
        assert svc.engine.execute("MATCH (n:Memory) RETURN count(n)").rows == [[256]]
        if mode == "crash":
            # Acknowledged graph + vector writes must survive an abrupt exit.
            os._exit(23)
    finally:
        svc.close()
    with Engine(cfg) as engine:
        assert not _hnsw(engine)
        assert engine.execute("MATCH (n:Memory) RETURN count(n)").rows == [[256]]
        assert engine.execute("MATCH (n:Memory) WHERE n.embedding IS NULL RETURN count(n)").rows == [[0]]
    print(json.dumps({"mode": mode, "rows": 256, "reopened": True}), flush=True)


def _child(mode, path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(  # noqa: S603 — fixed test script and explicit fixture paths
        [sys.executable, str(Path(__file__).resolve()), mode, str(path)],
        env=env, capture_output=True, text=True, timeout=90, check=False,
    )


@pytest.mark.parametrize("mode", ["text", "null", "config", "delete", "rollback", "reindex", "legacy"])
def test_vector_edits_and_reopen_do_not_crash(tmp_path, mode):
    result = _child(mode, tmp_path / "memory.lbdb")
    assert result.returncode == 0, f"child exit={result.returncode}\n{result.stdout}\n{result.stderr}"


def test_vector_writes_survive_process_exit_and_strict_recovery(tmp_path):
    from grag.recovery import recover_database

    cfg = _config(tmp_path / "memory.lbdb")
    result = _child("crash", cfg.db_path)
    assert result.returncode == 23, f"{result.returncode}\n{result.stdout}\n{result.stderr}"
    report = recover_database(cfg, out_dir=tmp_path / "recovery")
    assert not report["data_loss_possible"]
    with Engine(_config(Path(report["recovered_db"]))) as engine:
        assert not _hnsw(engine)
        assert engine.execute("MATCH (n:Memory) WHERE n.embedding IS NOT NULL RETURN count(n)").rows == [[256]]


def _legacy_file(path, *, custom=False):
    cfg = GragConfig(db_path=path, buffer_pool_size=128 * 1024**2)
    with Engine(cfg) as engine:
        engine.execute_write("CREATE NODE TABLE Memory(id STRING PRIMARY KEY, text STRING, embedding FLOAT[2])")
        engine.execute_write("CREATE (:Memory {id:'a', text:'remember orchids', embedding:[1.0, 0.0]})")
        engine.execute_write("CALL CREATE_FTS_INDEX('Memory', 'grag_fts__Memory', ['text'])")
        engine.execute_write("CALL CREATE_VECTOR_INDEX('Memory', 'grag_vec__Memory', 'embedding', metric := 'cosine')")
        if custom:
            engine.execute_write("CALL CREATE_VECTOR_INDEX('Memory', 'custom_vector', 'embedding', metric := 'cosine')")
    return cfg


def test_retire_legacy_index_with_embeddings_disabled_preserves_data_and_fts(tmp_path):
    cfg = _legacy_file(tmp_path / "old.lbdb")
    with Engine(cfg, read_only=True) as engine:
        before = engine.execute("MATCH (n:Memory) RETURN n").rows
    with Engine(cfg) as engine:
        assert cfg.embedder is None
        assert not _hnsw(engine)
        assert engine.execute("MATCH (n:Memory) RETURN n").rows == before
        assert engine.execute("CALL QUERY_FTS_INDEX('Memory', 'grag_fts__Memory', 'orchids', TOP := 2) RETURN node.id").rows == [["a"]]
        engine.execute_write("MATCH (n:Memory) SET n.text = 'updated', n.embedding = NULL")
    with Engine(cfg) as engine:
        assert not _hnsw(engine)
        assert engine.execute("MATCH (n:Memory) RETURN n.text, n.embedding").rows == [["updated", None]]


def test_read_only_open_preserves_legacy_index_and_file_bytes(tmp_path):
    cfg = _legacy_file(tmp_path / "old.lbdb")
    before = cfg.db_path.read_bytes()
    with Engine(cfg, read_only=True) as engine:
        assert len(_hnsw(engine)) == 1
        assert engine.execute("MATCH (n:Memory) RETURN n.id").rows == [["a"]]
    assert cfg.db_path.read_bytes() == before


def test_interrupted_legacy_index_retirement_strictly_replays(tmp_path):
    from grag.recovery import recover_database

    cfg = _legacy_file(tmp_path / "old.lbdb")
    script = """
import os, sys
from pathlib import Path
from grag.config import GragConfig
from grag.core.engine import Engine
original = Engine._run
def crash(self, conn, cypher, params):
    if cypher == 'CHECKPOINT':
        os._exit(24)
    return original(self, conn, cypher, params)
Engine._run = crash
Engine(GragConfig(db_path=Path(sys.argv[1]), buffer_pool_size=128*1024**2))
"""
    result = subprocess.run(  # noqa: S603 — fixed crash fixture
        [sys.executable, "-c", script, str(cfg.db_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 24, result.stderr
    report = recover_database(cfg, out_dir=tmp_path / "strict")
    assert not report["data_loss_possible"]
    assert report["tables"]["Memory"]["rows"] == 1


def test_unowned_native_index_refuses_writes_without_removing_any_index(tmp_path):
    cfg = _legacy_file(tmp_path / "old.lbdb", custom=True)
    with pytest.raises(ConfigurationError, match="not managed by grag"):
        Engine(cfg)
    with Engine(cfg, read_only=True) as engine:
        assert {r["index_name"] for r in _hnsw(engine)} == {"custom_vector", "grag_vec__Memory"}
        assert engine.execute("MATCH (n:Memory) RETURN n.text, n.embedding").rows == [["remember orchids", [1.0, 0.0]]]


@pytest.mark.parametrize("failure", ["inspect", "drop", "extension"])
def test_failed_index_retirement_never_exposes_a_writable_engine(tmp_path, monkeypatch, failure):
    cfg = _legacy_file(tmp_path / "old.lbdb")
    original = Engine._run

    def fail(self, conn, cypher, params):
        if failure == "inspect" and cypher.startswith("CALL SHOW_INDEXES"):
            raise CypherError("injected catalog failure")
        if failure == "drop" and cypher.startswith("CALL DROP_VECTOR_INDEX"):
            raise CypherError("injected removal failure")
        result = original(self, conn, cypher, params)
        if failure == "extension" and cypher.startswith("CALL SHOW_INDEXES"):
            column = result.columns.index("extension_loaded")
            for row in result.rows:
                if row[result.columns.index("index_type")] == "HNSW":
                    row[column] = False
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Engine, "_run", fail)
        with pytest.raises(ConfigurationError, match=r"Cannot (inspect|retire)"):
            Engine(cfg)
    with Engine(cfg, read_only=True) as engine:
        assert len(_hnsw(engine)) == 1
        assert engine.execute("MATCH (n:Memory) RETURN n.text").rows == [["remember orchids"]]


if __name__ == "__main__":
    _worker(sys.argv[1], Path(sys.argv[2]))
