"""Ingest generations remain complete across interruptions and concurrent callers."""

import importlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.types import CodeIngestRequest
from grag.ingest.code import ingest_code
from grag.transfer import export_lines

code = importlib.import_module("grag.ingest.code")


def _state(engine):
    """Portable graph state, excluding observation timestamps."""
    records = []
    for line in export_lines(engine):
        record = json.loads(line)
        if record["type"] == "grag_export":
            continue
        record.pop("created_at", None)
        if record["type"] == "node":
            record["properties"].pop("ingested_at", None)
        records.append(json.dumps(record, sort_keys=True))
    return sorted(records)


def _initial_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "core.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from core import helper\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    return root


def _change_repo(root):
    (root / "core.py").unlink()
    (root / "extra.py").write_text("def replacement():\n    return 2\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from extra import replacement\ndef run():\n    return replacement()\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("initially_indexed", [False, True])
@pytest.mark.parametrize("phase", [
    "upsert_nodes", "_prune_code_edges", "_prune_code_nodes",
    "_prune_legacy_repos", "upsert_edges", "_record_ingest_hashes",
])
def test_interruption_rolls_back_and_retry_matches_clean_ingest(
    engine, tmp_path, monkeypatch, initially_indexed, phase
):
    root = _initial_repo(tmp_path)
    req = CodeIngestRequest(paths=[str(root)])
    git_state = {"git_commit": "before", "git_branch": "main"}
    monkeypatch.setattr(code, "_git_state", lambda root: dict(git_state))
    if initially_indexed:
        ingest_code(engine, engine.config, req)
        before = _state(engine)
        _change_repo(root)
    git_state["git_commit"] = "after"
    real = getattr(code, phase)

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError(f"interrupted after {phase}")

    with monkeypatch.context() as fault:
        fault.setattr(code, phase, interrupted)
        with pytest.raises(RuntimeError, match="interrupted after"):
            ingest_code(engine, engine.config, req)

    if initially_indexed:
        assert _state(engine) == before
    else:
        # Schema preparation is deliberately outside the data transaction.
        for label in ("Repo", "Module", "Function"):
            assert engine.execute(f"MATCH (n:{label}) RETURN count(n)").rows == [[0]]

    retry = ingest_code(engine, engine.config, req)
    assert retry.files_unchanged == 0
    cfg = GragConfig(db_path=tmp_path / "clean.lbdb", buffer_pool_size=128 * 1024**2)
    with Engine(cfg) as reference:
        ingest_code(reference, cfg, req)
        assert _state(engine) == _state(reference)
    assert ingest_code(engine, engine.config, req).files_unchanged == 2


def test_source_revert_after_interruption_preserves_the_original_graph(
    engine, tmp_path, monkeypatch
):
    root = _initial_repo(tmp_path)
    req = CodeIngestRequest(paths=[str(root)])
    ingest_code(engine, engine.config, req)
    before = _state(engine)
    path = root / "core.py"
    original = path.read_text(encoding="utf-8")
    path.write_text("def changed():\n    return 2\n", encoding="utf-8")
    real = code._prune_code_nodes

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("after pruning")

    with monkeypatch.context() as fault:
        fault.setattr(code, "_prune_code_nodes", interrupted)
        with pytest.raises(RuntimeError, match="after pruning"):
            ingest_code(engine, engine.config, req)
    path.write_text(original, encoding="utf-8")
    assert ingest_code(engine, engine.config, req).files_unchanged == 2
    assert _state(engine) == before


def test_concurrent_ingest_cannot_publish_an_older_parse_last(engine, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "core.py"
    path.write_text("def old():\n    return 1\n", encoding="utf-8")
    req = CodeIngestRequest(paths=[str(root)])
    parsed_old = threading.Event()
    release_old = threading.Event()
    second_started = threading.Event()
    real = code._PARSERS[".py"]

    def controlled_parse(*args, **kwargs):
        result = real(*args, **kwargs)
        if "def old" in args[1]:
            parsed_old.set()
            assert release_old.wait(5)
        return result

    def second_ingest():
        second_started.set()
        return ingest_code(engine, engine.config, req)

    monkeypatch.setitem(code._PARSERS, ".py", controlled_parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ingest_code, engine, engine.config, req)
        try:
            assert parsed_old.wait(5)
            path.write_text("def new():\n    return 2\n", encoding="utf-8")
            second = pool.submit(second_ingest)
            assert second_started.wait(5)
            # Parsing does not hold the database writer lock.
            engine.execute_write("CREATE NODE TABLE Independent(id STRING PRIMARY KEY)")
        finally:
            release_old.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert engine.execute("MATCH (f:Function) RETURN f.name").rows == [["new"]]


def test_failed_ingest_remains_rolled_back_after_reopen(tmp_path, monkeypatch):
    root = _initial_repo(tmp_path)
    req = CodeIngestRequest(paths=[str(root)])
    cfg = GragConfig(db_path=tmp_path / "durable.lbdb", buffer_pool_size=128 * 1024**2)
    with Engine(cfg) as engine:
        ingest_code(engine, cfg, req)
        before = _state(engine)
        _change_repo(root)
        real = code.upsert_edges

        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("edge interruption")

        with monkeypatch.context() as fault:
            fault.setattr(code, "upsert_edges", interrupted)
            with pytest.raises(RuntimeError, match="edge interruption"):
                ingest_code(engine, cfg, req)
    with Engine(cfg) as reopened:
        assert _state(reopened) == before
        assert ingest_code(reopened, cfg, req).files_unchanged == 0


def test_abrupt_process_exit_preserves_last_committed_graph(tmp_path):
    root = _initial_repo(tmp_path)
    req = CodeIngestRequest(paths=[str(root)])
    cfg = GragConfig(db_path=tmp_path / "crash.lbdb", buffer_pool_size=128 * 1024**2)
    with Engine(cfg) as engine:
        ingest_code(engine, cfg, req)
        before = _state(engine)
    _change_repo(root)
    script = """
import importlib
import os
import sys
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.types import CodeIngestRequest
code = importlib.import_module('grag.ingest.code')
real = code.upsert_edges
def crash(*args, **kwargs):
    real(*args, **kwargs)
    os._exit(73)
code.upsert_edges = crash
cfg = GragConfig(db_path=sys.argv[1], buffer_pool_size=128 * 1024**2)
engine = Engine(cfg)
code.ingest_code(engine, cfg, CodeIngestRequest(paths=[sys.argv[2]]))
"""
    child = subprocess.run(  # noqa: S603 — fixed script over disposable fixtures
        [sys.executable, "-c", script, str(cfg.db_path), str(root)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert child.returncode == 73, child.stderr
    with Engine(cfg) as engine:
        assert _state(engine) == before
        assert ingest_code(engine, cfg, req).files_unchanged == 0
