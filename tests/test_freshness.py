"""M07 generation, retry, policy, transport, and race regressions."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grag.code_state import fingerprint, index_records
from grag.config import GragConfig
from grag.core.errors import FreshnessError, GragError
from grag.core.types import (
    CodeIngestRequest,
    ContextRequest,
    QueryRequest,
    ReadPolicy,
    SearchRequest,
)
from grag.mcp_server import server as mcp
from grag.service import GragService


def _write(root: Path, name: str, filename: str = "a.py") -> Path:
    root.mkdir(exist_ok=True)
    file = root / filename
    file.write_text(f"def {name}():\n    return 1\n")
    return file


def _fresh(svc: GragService, timeout: int = 3000):
    return svc.read_freshness(
        ReadPolicy(freshness="require", freshness_timeout_ms=timeout)
    )


def _names(svc: GragService) -> set[str]:
    return {r[0] for r in svc.engine.execute("MATCH (f:Function) RETURN f.name").rows}


def _root_status(svc: GragService, root: Path) -> dict:
    return next(
        r for r in svc.refresh_status(detail=True)["roots"] if r["path"] == str(root)
    )


@pytest.fixture()
def indexed(tmp_path):
    root = tmp_path / "code"
    _write(root, "alpha")
    svc = GragService(GragConfig(db_path=tmp_path / "fresh.lbdb", embedder=None))
    svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
    svc.enable_auto_refresh()
    svc.refresher.retry_base = 0.05
    svc.refresher.retry_max = 0.2
    assert _fresh(svc).status == "fresh"
    try:
        yield svc, root
    finally:
        svc.close()


def test_failed_generation_stays_pending_then_retries_without_another_edit(
    indexed, monkeypatch
):
    svc, root = indexed
    before = index_records(svc.engine)[str(root)]["_index_generation"]
    _write(root, "beta")
    real = svc.ingest_code
    attempts = []
    svc.refresher.retry_base = svc.refresher.retry_max = 0.2

    def fail(req):
        attempts.append(req)
        raise GragError("temporary disk failure")

    monkeypatch.setattr(svc, "ingest_code", fail)
    report = svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=80))
    assert report.status == "error" and report.timed_out
    state = _root_status(svc, root)
    assert state["successful_generation"] == before
    assert state["pending_generation"] == state["observed_generation"] != before
    assert state["failures"] == 1 and "disk failure" in state["error"]
    for _ in range(5):
        assert svc.read_freshness().status == "error"
    assert len(attempts) == 1
    assert _names(svc) == {"alpha"}
    monkeypatch.setattr(svc, "ingest_code", real)
    assert _fresh(svc).status == "fresh"
    state = _root_status(svc, root)
    assert state["successful_generation"] == state["observed_generation"] != before
    assert state["pending_generation"] is None and state["error"] is None
    assert state["failures"] == 0 and _names(svc) == {"beta"}


def test_queue_failure_is_visible_and_retried(indexed, monkeypatch):
    svc, _ = indexed
    real = svc.jobs.submit
    attempts = 0

    def submit(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise GragError("queue unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(svc.jobs, "submit", submit)
    report = svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=0))
    assert report.status == "error" and report.timed_out
    assert "queue unavailable" in svc.refresh_status(detail=True)["last_error"]
    assert _fresh(svc).status == "fresh"
    assert attempts == 2


def test_catalog_check_failure_is_visible_and_retried(indexed, monkeypatch):
    import grag.refresh as refresh

    svc, _ = indexed
    real = refresh.index_records
    attempts = 0

    def records(engine):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise GragError("catalog temporarily unavailable")
        return real(engine)

    monkeypatch.setattr(refresh, "index_records", records)
    report = svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=20))
    assert report.status == "error" and report.timed_out
    assert "catalog temporarily" in svc.refresh_status(detail=True)["last_error"]
    assert _fresh(svc).status == "fresh"
    assert attempts == 2


def test_exception_after_commit_rechecks_without_repeating_ingest(indexed, monkeypatch):
    svc, root = indexed
    _write(root, "beta")
    real = svc.ingest_code
    calls = []

    def ingest(req):
        calls.append(req)
        real(req)
        raise GragError("response failed after commit")

    monkeypatch.setattr(svc, "ingest_code", ingest)
    assert _fresh(svc).status == "fresh"
    assert len(calls) == 1 and _names(svc) == {"beta"}


@pytest.mark.parametrize("code_columns", [False, True])
def test_authored_repo_entities_do_not_enroll_source_paths(tmp_path, code_columns):
    svc = GragService(GragConfig(db_path=tmp_path / "authored.lbdb", embedder=None))
    try:
        extra = ", ingested_at TIMESTAMP" if code_columns else ""
        svc.engine.execute_write(
            f"CREATE NODE TABLE Repo(id STRING, path STRING{extra}, PRIMARY KEY(id))"
        )
        svc.engine.execute_write(
            "CREATE (:Repo {id:'authored', path:'/no/local/checkout'})"
        )
        svc.enable_auto_refresh()
        assert _fresh(svc).status == "fresh"
        assert svc.refresh_status(detail=True)["roots"] == []
    finally:
        svc.close()


def test_legacy_code_containment_without_ingest_stamp_is_unknown(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "legacy.lbdb", embedder=None))
    try:
        svc.engine.execute_write(
            "CREATE NODE TABLE Repo(id STRING, path STRING, PRIMARY KEY(id))"
        )
        svc.engine.execute_write("CREATE NODE TABLE Module(id STRING, PRIMARY KEY(id))")
        svc.engine.execute_write(
            "CREATE REL TABLE CONTAINS_REPO_MODULE(FROM Repo TO Module)"
        )
        svc.engine.execute_write("CREATE (:Repo {id:'legacy', path:'/old/code'})")
        svc.engine.execute_write("CREATE (:Repo {id:'authored', path:'/not/indexed'})")
        svc.engine.execute_write("CREATE (:Module {id:'module'})")
        svc.engine.execute_write(
            "MATCH (r:Repo {id:'legacy'}), (m:Module) CREATE (r)-[:CONTAINS_REPO_MODULE]->(m)"
        )
        svc.enable_auto_refresh()
        with pytest.raises(FreshnessError) as exc:
            _fresh(svc, timeout=50)
        assert exc.value.freshness["status"] == "unknown"
        assert [r["path"] for r in svc.refresh_status(detail=True)["roots"]] == [
            "/old/code"
        ]
        assert svc.refresher.refreshes == 0
    finally:
        svc.close()


def test_new_scope_registered_during_check_must_also_be_verified(indexed, monkeypatch):
    import grag.refresh as refresh

    svc, root = indexed
    real = refresh.scan_sources
    entered, release = threading.Event(), threading.Event()

    def scan(*args):
        result = real(*args)
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(refresh, "scan_sources", scan)
    svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=0))
    try:
        assert entered.wait(5)
        other = root.with_name("new_scope")
        _write(other, "invalid").write_text("def invalid(:\n")
        svc.ingest_code(CodeIngestRequest(paths=[str(other)]))
    finally:
        release.set()
    with svc.refresher._condition:
        assert svc.refresher._condition.wait_for(
            lambda: not svc.refresher._checking, timeout=5
        )
        # A completed check of the old catalog cannot certify the new scope.
        assert svc.refresher._report().status != "fresh"
    with pytest.raises(FreshnessError):
        _fresh(svc, timeout=0)
    with svc.refresher._condition:
        assert svc.refresher._condition.wait_for(
            lambda: not svc.refresher._checking, timeout=5
        )
    assert svc.refresh_status(detail=True)["tracked"] == 2


def test_missing_registered_file_does_not_expand_to_its_parent(indexed):
    svc, root = indexed
    # Replace the previous folder registration with an explicitly saved file
    # policy, as if this database had originally indexed only this file.
    req = CodeIngestRequest(paths=[str(root / "a.py")])
    svc.engine.execute_write(
        "MATCH (r:Repo) SET r._index_options=$policy_json",
        {"policy_json": json.dumps(req.model_dump())},
    )
    (root / "a.py").unlink()
    _write(root, "unrequested", "b.py")
    with pytest.raises(FreshnessError):
        _fresh(svc, timeout=80)
    assert _names(svc) == {"alpha"}
    assert svc.refresher.refreshes == 0


@pytest.mark.parametrize("already_indexed", [False, True])
def test_size_exclusions_cannot_claim_retained_old_symbols_fresh(
    indexed, already_indexed
):
    svc, root = indexed
    svc.ingest_code(CodeIngestRequest(paths=[str(root)], max_file_kb=1))
    file = root / ("a.py" if already_indexed else "oversized.py")
    file.write_text("#" + "x" * 2048 + "\ndef changed():\n    pass\n")
    if already_indexed:
        with pytest.raises(FreshnessError):
            _fresh(svc, timeout=80)
    else:
        assert _fresh(svc).status == "fresh"
    assert _names(svc) == {"alpha"}


def test_backoff_grows_and_is_capped(indexed, monkeypatch):
    import grag.refresh as refresh

    svc, root = indexed
    _write(root, "beta")
    svc.refresher.retry_base = 1.0
    svc.refresher.retry_max = 2.0
    # Freeze only the refresher's clock; condition waits retain real deadlines.
    monkeypatch.setattr(refresh, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(
        svc, "ingest_code", lambda req: (_ for _ in ()).throw(RuntimeError("failed"))
    )
    for failures, expected in [(1, 1.0), (2, 2.0), (3, 2.0)]:
        # Advance only this root's retry clock; avoid slow wall-clock tests.
        with svc.refresher._condition:
            svc.refresher._roots[str(root)].retry_at = 0.0
        svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=0))
        with svc.refresher._condition:
            assert svc.refresher._condition.wait_for(
                lambda: not svc.refresher._checking, timeout=5
            )
        state = _root_status(svc, root)
        assert state["failures"] == failures
        assert state["retry_in_s"] == expected


def test_content_fingerprint_detects_non_newest_and_restored_mtimes(tmp_path):
    root = tmp_path / "plain"
    older = _write(root, "alpha", "older.py")
    newer = _write(root, "newer", "newer.py")
    os.utime(older, ns=(1_000_000, 1_000_000))
    os.utime(newer, ns=(9_000_000, 9_000_000))
    before = fingerprint(root)
    _write(root, "gamma", "older.py")
    os.utime(older, ns=(1_000_000, 1_000_000))
    assert fingerprint(root) != before
    before = fingerprint(root)
    older.rename(root / "renamed 'name'.py")
    assert fingerprint(root) != before


def test_parse_failure_cannot_advance_successful_generation(indexed):
    svc, root = indexed
    before = index_records(svc.engine)[str(root)]["_index_generation"]
    (root / "a.py").write_text("def invalid(:\n")
    result = svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
    assert result.warnings
    stored = index_records(svc.engine)[str(root)]
    assert stored["_index_generation"] == before
    assert stored["_index_error"]
    with pytest.raises(FreshnessError):
        _fresh(svc, timeout=60)
    assert _names(svc) == {"alpha"}
    _write(root, "repaired")
    assert _fresh(svc).status == "fresh"
    assert _names(svc) == {"repaired"}


def test_source_edit_during_parse_is_verified_again(indexed, monkeypatch):
    import grag.ingest.code as code

    svc, root = indexed
    before = index_records(svc.engine)[str(root)]["_index_generation"]
    _write(root, "beta")
    real = code._PARSERS[".py"]

    def parse(*args, **kwargs):
        parsed = real(*args, **kwargs)
        _write(root, "gamma")
        return parsed

    with monkeypatch.context() as patch:
        patch.setitem(code._PARSERS, ".py", parse)
        svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
    assert index_records(svc.engine)[str(root)]["_index_generation"] == before
    assert _names(svc) == {"beta"}
    assert _fresh(svc).status == "fresh"
    assert _names(svc) == {"gamma"}


def test_source_edit_during_commit_does_not_bless_the_queued_generation(
    indexed, monkeypatch
):
    import grag.ingest.code as code

    svc, root = indexed
    _write(root, "beta")
    real = code.record_generations
    calls = 0

    def record(*args, **kwargs):
        nonlocal calls
        real(*args, **kwargs)
        calls += 1
        if calls == 1:
            _write(root, "gamma")

    monkeypatch.setattr(code, "record_generations", record)
    assert _fresh(svc).status == "fresh"
    assert calls == 2 and _names(svc) == {"gamma"}
    state = _root_status(svc, root)
    assert state["observed_generation"] == state["successful_generation"]
    assert state["pending_generation"] is None


def test_final_generation_write_rolls_back_with_the_graph(indexed, monkeypatch):
    import grag.ingest.code as code

    svc, root = indexed
    before = index_records(svc.engine)
    _write(root, "beta")
    real = code.record_generations

    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise GragError("after generation write")

    with monkeypatch.context() as patch:
        patch.setattr(code, "record_generations", fail)
        with pytest.raises(GragError, match="generation write"):
            svc.ingest_code(CodeIngestRequest(paths=[str(root)], calls=False))
    assert index_records(svc.engine) == before
    assert _names(svc) == {"alpha"}
    assert _fresh(svc).status == "fresh"


def test_options_and_file_scope_survive_restart(tmp_path):
    root = tmp_path / "code"
    a = _write(root, "alpha")
    _write(root, "unrequested", "b.py")
    config = GragConfig(db_path=tmp_path / "restart.lbdb", embedder=None)
    svc = GragService(config)
    req = CodeIngestRequest(
        paths=[str(a)], calls=False, max_file_kb=4, incremental=False
    )
    svc.ingest_code(req)
    svc.close()
    svc = GragService(config)
    try:
        svc.enable_auto_refresh()
        assert _fresh(svc).status == "fresh"
        assert svc.refresher.refreshes == 0
        _write(root, "beta")
        assert _fresh(svc).status == "fresh"
        assert _names(svc) == {"beta"}
        assert (
            json.loads(index_records(svc.engine)[str(root)]["_index_options"])
            == req.model_dump()
        )
    finally:
        svc.close()


def test_partial_ingest_keeps_registered_scope_and_latest_options(indexed):
    svc, root = indexed
    _write(root, "other", "b.py")
    svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
    svc.ingest_code(
        CodeIngestRequest(paths=[str(root / "a.py")], calls=False, max_file_kb=8)
    )
    stored = index_records(svc.engine)[str(root)]
    assert json.loads(stored["_index_options"])["paths"] == [str(root)]
    assert stored["_index_error"]  # only part of the registered scope was parsed
    assert _fresh(svc).status == "fresh"
    assert _names(svc) == {"alpha", "other"}
    assert (
        json.loads(index_records(svc.engine)[str(root)]["_index_options"])["calls"]
        is False
    )


def test_one_failed_root_does_not_block_another(indexed, tmp_path, monkeypatch):
    svc, root = indexed
    other = tmp_path / "other"
    _write(other, "other_before")
    svc.ingest_code(CodeIngestRequest(paths=[str(other)], calls=False))
    _fresh(svc)
    _write(root, "beta")
    _write(other, "other_after")
    real = svc.ingest_code
    svc.refresher.retry_base = svc.refresher.retry_max = 5.0
    healthy_done = threading.Event()

    def ingest(req):
        if req.paths == [str(root)]:
            raise GragError("broken first root")
        result = real(req)
        healthy_done.set()
        return result

    monkeypatch.setattr(svc, "ingest_code", ingest)
    svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=0))
    assert healthy_done.wait(3)
    with svc.refresher._condition:
        assert svc.refresher._condition.wait_for(
            lambda: not svc.refresher._checking, timeout=3
        )
    assert svc.refresh_status()["freshness"]["status"] == "error"
    assert _names(svc) == {"alpha", "other_after"}
    assert _root_status(svc, root)["error"]
    assert _root_status(svc, other)["pending_generation"] is None
    assert _root_status(svc, other)["error"] is None


@pytest.mark.parametrize(
    "damage, status",
    [("legacy", "unknown"), ("invalid", "error"), ("missing", "error")],
)
def test_unverifiable_indexes_stay_visible_and_do_not_guess_a_scope(
    indexed, damage, status
):
    svc, root = indexed
    if damage == "legacy":
        svc.engine.execute_write("MATCH (r:Repo) SET r._index_options=NULL")
    elif damage == "invalid":
        svc.engine.execute_write("MATCH (r:Repo) SET r._index_options='invalid json'")
    else:
        root.rename(root.with_name("relocated"))
    with pytest.raises(FreshnessError) as exc:
        _fresh(svc, timeout=50)
    assert exc.value.freshness["status"] == status
    assert svc.read_freshness().status == status
    assert svc.refresher.refreshes == 0
    assert _names(svc) == {"alpha"}


def test_wait_deadline_covers_slow_checks_and_require_does_not_query(
    indexed, monkeypatch
):
    import grag.refresh as refresh

    svc, _ = indexed
    real = refresh.scan_sources
    entered, release = threading.Event(), threading.Event()
    queried = []
    execute = svc.engine.execute

    def scan(*args):
        entered.set()
        assert release.wait(5)
        return real(*args)

    def tracked(query, *args, **kwargs):
        if "RETURN 999" in query:
            queried.append(query)
        return execute(query, *args, **kwargs)

    monkeypatch.setattr(refresh, "scan_sources", scan)
    monkeypatch.setattr(svc.engine, "execute", tracked)
    try:
        started = time.monotonic()
        report = svc.read_freshness(
            ReadPolicy(freshness="wait", freshness_timeout_ms=30)
        )
        assert report.timed_out and report.status == "checking"
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        with pytest.raises(FreshnessError):
            svc.cypher_query(
                QueryRequest(
                    cypher="RETURN 999", freshness="require", freshness_timeout_ms=20
                )
            )
        assert queried == []
        stale = svc.cypher_query(QueryRequest(cypher="RETURN 999"))
        assert stale.rows == [[999]] and stale.freshness.status == "checking"
    finally:
        release.set()
    assert _fresh(svc).status == "fresh"


def test_concurrent_readers_share_one_check(indexed, monkeypatch):
    import grag.refresh as refresh

    svc, _ = indexed
    real = refresh.scan_sources
    entered, release = threading.Event(), threading.Event()
    calls = []

    def scan(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return real(*args)

    monkeypatch.setattr(refresh, "scan_sources", scan)
    with ThreadPoolExecutor(max_workers=8) as pool:
        try:
            first = pool.submit(_fresh, svc)
            assert entered.wait(1)
            others = [
                pool.submit(
                    svc.read_freshness,
                    ReadPolicy(freshness="wait", freshness_timeout_ms=50),
                )
                for _ in range(7)
            ]
            assert all(f.result(timeout=1).timed_out for f in others)
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=3).status == "fresh"


def test_disabled_require_fails_without_reading_graph(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "disabled.lbdb"))
    try:
        assert svc.read_freshness().status == "disabled"
        with pytest.raises(FreshnessError, match="disabled"):
            svc.cypher_query(
                QueryRequest(cypher="MATCH (n:Missing) RETURN n", freshness="require")
            )
    finally:
        svc.close()


def test_explicit_policy_change_wins_over_a_queued_refresh(indexed, monkeypatch):
    svc, root = indexed
    _write(root, "beta")
    real = svc.ingest_code
    calls = []

    def ingest(req):
        calls.append(req.calls)
        return real(req)

    monkeypatch.setattr(svc, "ingest_code", ingest)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with svc.engine.code_ingest_lock:
            future = pool.submit(_fresh, svc)
            with svc.refresher._condition:
                assert svc.refresher._condition.wait_for(
                    lambda: svc.refresher._running_root is not None,
                    timeout=2,
                )
            svc.ingest_code(
                CodeIngestRequest(paths=[str(root)], calls=False, max_file_kb=8)
            )
        assert future.result(timeout=3).status == "fresh"
    assert calls == [False]
    assert (
        json.loads(index_records(svc.engine)[str(root)]["_index_options"])["calls"]
        is False
    )


def test_close_drains_owned_verification_before_closing_engine(indexed, monkeypatch):
    import grag.refresh as refresh

    svc, _ = indexed
    real = refresh.scan_sources
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def scan(*args):
        entered.set()
        assert release.wait(5)
        return real(*args)

    monkeypatch.setattr(refresh, "scan_sources", scan)
    svc.refresher.invalidate()
    svc.read_freshness()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (svc.close(), closed.set()))
    closer.start()
    try:
        assert not closed.wait(0.03)
        assert svc.engine.execute("RETURN 1").rows == [[1]]
    finally:
        release.set()
        closer.join(timeout=3)
    assert closed.is_set()


@pytest.mark.parametrize(
    "surface", ["schema", "query", "search", "context", "sample", "full"]
)
def test_python_graph_reads_expose_freshness_and_require_it(indexed, surface):
    svc, root = indexed
    _write(root, "beta")

    def read(mode):
        opts = {
            "freshness": mode,
            "freshness_timeout_ms": 60 if mode == "wait" else 3000,
        }
        if surface == "schema":
            return svc.describe_schema(**opts)
        if surface == "query":
            return svc.cypher_query(
                QueryRequest(cypher="MATCH (f:Function) RETURN f.name", **opts)
            )
        if surface == "search":
            return svc.search_knowledge(
                SearchRequest(query="beta", token_budget=512, **opts)
            )
        if surface == "context":
            return svc.get_context(
                ContextRequest(node_ids=[], token_budget=256, **opts)
            )
        if surface == "sample":
            return svc.graph_sample(**opts)
        return svc.graph_full(**opts)

    result = read("require")
    assert result.freshness.status == "fresh" and result.freshness.checked_at
    if surface in {"search", "context"}:
        assert result.response_token_estimate <= (512 if surface == "search" else 256)
    (root / "a.py").write_text("def broken(:\n")
    result = read("wait")
    assert result.freshness.status != "fresh" and result.freshness.timed_out


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("describe_schema", {}),
        ("cypher_query", {"cypher": "RETURN 3"}),
        ("search_knowledge", {"query": "beta", "token_budget": 512}),
        ("get_context", {"node_ids": [], "token_budget": 256}),
    ],
)
def test_registered_mcp_read_policies_and_metadata(tmp_path, name, arguments):
    root = tmp_path / "code"
    _write(root, "alpha")
    server = mcp.create_server(GragConfig(db_path=tmp_path / "mcp.lbdb", embedder=None))
    svc = server.grag_service
    try:
        svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
        _fresh(svc)
        _write(root, "beta")
        out = asyncio.run(server.call_tool(name, {**arguments, "freshness": "require"}))
        text = out.content[0].text
        assert not text.startswith("ERROR"), text
        payload = json.loads(text.rpartition("\n---\n")[2])
        assert payload["freshness"]["status"] == "fresh"
        if "token_budget" in arguments:
            assert (len(text.encode()) + 3) // 4 <= arguments["token_budget"]
        tool = next(t for t in asyncio.run(server.list_tools()) if t.name == name)
        assert "freshness_timeout_ms" in tool.description
        assert "Legacy indexes" in tool.description
        assert set(tool.input_schema["properties"]["freshness"]["enum"]) == {
            "allow_stale",
            "wait",
            "require",
        }
        (root / "a.py").write_text("def broken(:\n")
        out = asyncio.run(
            server.call_tool(
                name,
                {
                    **arguments,
                    "freshness": "require",
                    "freshness_timeout_ms": 50,
                },
            )
        )
        text = out.content[0].text
        assert text.startswith("ERROR"), text
        payload = json.loads(text.rpartition("\n---\n")[2])
        assert payload["code"] == "freshness_unavailable"
        assert payload["freshness"]["status"] != "fresh"
    finally:
        svc.close()


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/schema", None),
        ("/api/schema?format=text", None),
        ("/api/graph/sample", None),
        ("/api/graph/full", None),
        ("/api/index/status", None),
        ("/api/export", None),
        ("/api/query", {"cypher": "RETURN 7"}),
        ("/api/search", {"query": "beta", "token_budget": 512}),
        ("/api/context", {"node_ids": [], "token_budget": 256}),
    ],
)
def test_rest_read_policies_and_metadata(tmp_path, path, body):
    from grag.api.main import create_app

    root = tmp_path / "code"
    _write(root, "alpha")
    app = create_app(GragConfig(db_path=tmp_path / "rest.lbdb", embedder=None))
    with TestClient(app) as client:
        svc = app.state.service
        svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
        _write(root, "beta")

        # Verify REST policy/metadata behavior, not native indexing speed on
        # a busy CI runner. The failure case below keeps its explicit 50 ms.
        def read(mode, timeout=30_000):
            policy = {"freshness": mode, "freshness_timeout_ms": timeout}
            return (
                client.get(path, params=policy)
                if body is None
                else client.post(path, json={**body, **policy})
            )

        response = read("require")
        assert response.status_code == 200, response.text
        if path == "/api/export":
            report = json.loads(response.headers["x-grag-freshness"])
        elif path.endswith("format=text"):
            report = json.loads(response.text.rpartition("\n---\n")[2])["freshness"]
        else:
            report = response.json()["freshness"]
        assert report["status"] == "fresh" and report["checked_at"]
        if body and "token_budget" in body:
            assert (len(response.content) + 3) // 4 <= body["token_budget"]
        (root / "a.py").write_text("def broken(:\n")
        response = read("require", 50)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "freshness_unavailable"
        assert response.json()["freshness"]["status"] != "fresh"


def test_detailed_paths_and_errors_require_authentication(tmp_path):
    from grag.api.main import create_app

    root = tmp_path / "private-code"
    _write(root, "alpha")
    app = create_app(
        GragConfig(
            db_path=tmp_path / "auth.lbdb",
            api_token="test-secret",  # noqa: S106 — test credential
            embedder=None,
        )
    )
    with TestClient(app) as client:
        svc = app.state.service
        svc.ingest_code(CodeIngestRequest(paths=[str(root)]))
        svc.engine.execute_write("MATCH (r:Repo) SET r._index_options=NULL")
        svc.read_freshness(ReadPolicy(freshness="wait", freshness_timeout_ms=20))
        health = client.get("/api/health")
        assert str(root) not in health.text
        assert health.json()["code_index"]["freshness"]["status"] == "unknown"
        assert client.get("/api/index/status").status_code == 401
        detail = client.get(
            "/api/index/status", headers={"Authorization": "Bearer test-secret"}
        )
        assert detail.status_code == 200 and detail.json()["roots"][0]["path"] == str(
            root
        )


@pytest.mark.parametrize("bad", ["invalid", "REQUIRE", ""])
def test_invalid_rest_freshness_mode_is_rejected(tmp_path, bad):
    from grag.api.main import create_app

    with TestClient(create_app(GragConfig(db_path=tmp_path / "policy.lbdb"))) as client:
        assert client.get("/api/schema", params={"freshness": bad}).status_code == 422
        assert (
            client.post(
                "/api/query", json={"cypher": "RETURN 1", "freshness": bad}
            ).status_code
            == 422
        )


@pytest.mark.parametrize("timeout", [-1, 60_001])
def test_rest_wait_deadlines_are_validated(tmp_path, timeout):
    from grag.api.main import create_app

    with TestClient(
        create_app(GragConfig(db_path=tmp_path / "deadline.lbdb"))
    ) as client:
        assert (
            client.get(
                "/api/graph/full", params={"freshness_timeout_ms": timeout}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/context", json={"node_ids": [], "freshness_timeout_ms": timeout}
            ).status_code
            == 422
        )
