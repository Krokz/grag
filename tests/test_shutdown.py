"""Shutdown drills with live native databases, blocked work and concurrent callers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from grag.api.main import create_app
from grag.config import EmbedderConfig, GragConfig
from grag.core.errors import ShutdownError
from grag.core.types import CodeIngestRequest, QueryRequest, ReadPolicy
from grag.embedworker import EmbedWorker, attached_worker, notify_embed_worker
from grag.jobs import JobManager
from grag.mcp_server import server as mcp_server
from grag.registry import ServiceRegistry
from grag.service import GragService
from test_vectors import FAKE_DIM, FakeEmbedder, make_docs


@pytest.fixture()
def svc(tmp_path):
    service = GragService(
        GragConfig(
            db_path=tmp_path / "shutdown.lbdb", buffer_pool_size=128 * 1024 * 1024
        )
    )
    yield service
    assert service.close(timeout=5)["engine_closed"]


def test_queued_jobs_cancel_and_active_work_commits_before_close(svc):
    entered, release = threading.Event(), threading.Event()
    svc.engine.execute_write("CREATE NODE TABLE Note(id STRING PRIMARY KEY)")

    def work():
        entered.set()
        assert release.wait(5)
        svc.engine.execute_write("CREATE (:Note {id:'saved'})")
        return {"saved": True}

    active = svc.jobs.submit("probe", work, {})
    assert entered.wait(2)
    queued = svc.jobs.submit("must-cancel", lambda: pytest.fail("queued work ran"), {})
    try:
        start = time.monotonic()
        result = svc.close(timeout=0.05)
        assert time.monotonic() - start < 1
        assert result["timed_out"] and not result["engine_closed"]
        assert result["jobs"]["active"] == [active.id]
        cancelled = svc.get_job(queued.id)
        assert cancelled.status == "cancelled" and cancelled.finished_at
        assert cancelled.started_at is None and cancelled.error
        assert svc.engine.execute("MATCH (n:Note) RETURN n.id").rows == []
        with pytest.raises(ShutdownError):
            svc.submit_ingest_code(CodeIngestRequest(paths=["unused"]))
        with pytest.raises(ShutdownError):
            svc.cypher_query(QueryRequest(cypher="RETURN 1"))
    finally:
        release.set()
    assert svc.close(timeout=3)["engine_closed"]
    assert svc.get_job(active.id).status == "done"
    reopened = GragService(svc.config)
    try:
        assert reopened.cypher_query(
            QueryRequest(cypher="MATCH (n:Note) RETURN n.id")
        ).rows == [["saved"]]
    finally:
        reopened.close()


def test_real_ingest_finishes_after_shutdown_timeout(svc, tmp_path, monkeypatch):
    import grag.ingest.code as code

    source = tmp_path / "example.py"
    source.write_text("def saved():\n    return 1\n")
    entered, release = threading.Event(), threading.Event()
    parse = code._PARSERS[".py"]

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return parse(*args, **kwargs)

    monkeypatch.setitem(code._PARSERS, ".py", paused)
    job = svc.submit_ingest_code(CodeIngestRequest(paths=[str(source)]))
    assert entered.wait(2)
    try:
        result = svc.close(timeout=0.03)
        assert result["active_operations"] == 1 and not result["engine_closed"]
    finally:
        release.set()
    assert svc.close(timeout=3)["engine_closed"]
    done = svc.get_job(job.id)
    assert done.status == "done" and done.result["functions"] == 1
    reopened = GragService(svc.config)
    try:
        assert reopened.engine.execute("MATCH (f:Function) RETURN f.name").rows == [
            ["saved"]
        ]
    finally:
        reopened.close()


def test_stuck_embedding_retains_live_handle_and_database(svc, monkeypatch):
    from grag.retrieval import vectors

    make_docs(svc.engine)
    svc.config.embedder = EmbedderConfig(
        provider="fastembed", model="fake", dim=FAKE_DIM
    )
    entered, release = threading.Event(), threading.Event()

    class SlowEmbedder(FakeEmbedder):
        def embed(self, texts):
            entered.set()
            assert release.wait(5)
            return super().embed(texts)

    monkeypatch.setattr(vectors, "get_embedder", lambda config: SlowEmbedder())
    svc.start_background_embedding()
    worker = svc.embed_worker
    assert entered.wait(2)
    try:
        assert worker.stop(timeout=0.01) is False
        assert worker.running and worker.status()["stopping"]
        assert attached_worker(svc.engine) is worker
        result = svc.close(timeout=0.03)
        assert result["embedding_running"] and not result["engine_closed"]
        assert svc.embed_worker is worker
        assert svc.engine.execute("MATCH (n:Doc) RETURN count(n)").rows == [[3]]
    finally:
        release.set()
    assert svc.close(timeout=3)["engine_closed"]
    assert not worker.running and worker.last_error is None
    assert worker.stop(timeout=0) is True


def test_stopped_worker_never_triggers_inline_embedding(svc):
    worker = EmbedWorker(svc.engine, svc.config)
    svc.engine.embed_worker = worker
    assert worker.stop(timeout=0)
    worker.start()
    assert not worker.running
    assert notify_embed_worker(svc.engine)  # pending work stays pending for restart
    assert attached_worker(svc.engine) is worker
    assert worker.drain_once() == 0


def test_active_read_keeps_engine_until_native_result_returns(svc, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    execute = svc.engine.execute

    def paused(cypher, *args, **kwargs):
        if cypher.startswith("RETURN 42"):
            entered.set()
            assert release.wait(5)
        return execute(cypher, *args, **kwargs)

    monkeypatch.setattr(svc.engine, "execute", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(svc.cypher_query, QueryRequest(cypher="RETURN 42"))
        assert entered.wait(2)
        try:
            assert not svc.close(timeout=0.03)["engine_closed"]
        finally:
            release.set()
        assert read.result(timeout=3).rows == [[42]]
    assert svc.close(timeout=3)["engine_closed"]


def test_nested_operation_can_complete_but_new_calls_are_refused(svc):
    with svc.operation():
        report = svc.close(timeout=0)
        assert report["active_operations"] == 1
        assert svc.cypher_query(QueryRequest(cypher="RETURN 1")).rows == [[1]]
    assert svc.close(timeout=3)["engine_closed"]
    for call in (
        svc.describe_schema,
        svc.start_background_embedding,
        svc.enable_auto_refresh,
    ):
        with pytest.raises(ShutdownError):
            call()


def test_concurrent_close_finalizes_engine_once(svc, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_close = svc.engine.close
    calls = []

    def paused():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        real_close()

    monkeypatch.setattr(svc.engine, "close", paused)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(svc.close, 3)
        assert entered.wait(2)
        others = [pool.submit(svc.close, 3) for _ in range(2)]
        release.set()
        assert all(f.result(timeout=4)["engine_closed"] for f in [first, *others])
    assert calls == [1]
    assert svc.close(timeout=0)["state"] == "closed"


def test_shutdown_failure_is_reported(tmp_path, monkeypatch):
    svc = GragService(GragConfig(db_path=tmp_path / "failed.lbdb"))
    real_close = svc.engine.close
    monkeypatch.setattr(
        svc.engine, "close", lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
    )
    try:
        result = svc.close(timeout=3)
        assert result["state"] == "error" and not result["engine_closed"]
        assert result["error"] == "RuntimeError: close failed"
        assert not result["timed_out"]
    finally:
        real_close()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_deadline_does_not_start_shutdown(svc, timeout):
    with pytest.raises(ValueError, match="finite"):
        svc.close(timeout=timeout)
    assert svc.shutdown_status()["state"] == "open"


def test_submit_and_shutdown_have_no_orphaned_records(monkeypatch):
    manager = JobManager()
    entered, release = threading.Event(), threading.Event()
    submit = manager._pool.submit

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return submit(*args, **kwargs)

    monkeypatch.setattr(manager._pool, "submit", paused)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(manager.submit, "race", lambda: {}, {})
            assert entered.wait(2)
            shutdown = pool.submit(manager.shutdown, True)
            release.set()
            job = submission.result(timeout=3)
            shutdown.result(timeout=3)
        assert manager.get(job.id).status in ("done", "cancelled")
        assert manager.shutdown_status()["active"] == []
        with pytest.raises(ShutdownError):
            manager.submit("late", lambda: {}, {})
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_executor_submission_failure_is_terminal(monkeypatch):
    manager = JobManager()
    monkeypatch.setattr(
        manager._pool,
        "submit",
        lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    try:
        from grag.core.errors import GragError

        with pytest.raises(GragError, match="could not be submitted"):
            manager.submit("rejected", lambda: {}, {})
        assert manager.list()[0].status == "failed"
        assert manager.list()[0].finished_at
    finally:
        manager.shutdown(wait=True)


def test_base_exception_cannot_leave_job_running():
    manager = JobManager()
    entered = threading.Event()

    def fail():
        entered.set()
        raise SystemExit("worker stopped")

    try:
        job = manager.submit("failure", fail, {})
        assert entered.wait(2)
        manager.shutdown(wait=True)
        result = manager.get(job.id)
        assert (
            result.status == "failed" and result.error == "SystemExit: worker stopped"
        )
    finally:
        manager.shutdown(wait=True)


def test_registry_uses_one_deadline_and_cannot_reopen_during_drain(tmp_path):
    registry = ServiceRegistry(GragConfig(db_dir=tmp_path))
    # Explicitly create the files; registry get() deliberately does not create them.
    for name in ("a", "b"):
        seed = GragService(GragConfig(db_path=tmp_path / f"{name}.lbdb"))
        seed.close()
    services = [registry.get(name) for name in ("a", "b")]
    release = threading.Event()
    started = [threading.Event(), threading.Event()]
    for service, event in zip(services, started, strict=True):

        def hold(event=event):
            event.set()
            assert release.wait(5)
            return {}

        service.jobs.submit("hold", hold, {})
    assert all(event.wait(2) for event in started)
    try:
        start = time.monotonic()
        reports = registry.close(timeout=0.05)
        assert time.monotonic() - start < 0.5
        assert len(reports) == 2 and all(r["timed_out"] for r in reports.values())
        with pytest.raises(ShutdownError):
            registry.get("a")
        assert all(
            service.shutdown_status()["state"] == "draining" for service in services
        )
    finally:
        release.set()
        assert all(r["engine_closed"] for r in registry.close(timeout=3).values())


def test_verification_cancels_and_wakes_required_readers(svc, tmp_path, monkeypatch):
    import grag.refresh as refresh

    source = tmp_path / "example.py"
    source.write_text("def before(): pass\n")
    svc.ingest_code(CodeIngestRequest(paths=[str(source)]))
    svc.enable_auto_refresh()
    entered, release = threading.Event(), threading.Event()
    scan = refresh.scan_sources

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return scan(*args)

    monkeypatch.setattr(refresh, "scan_sources", paused)
    svc.read_freshness()
    assert entered.wait(2)
    try:
        assert not svc.close(timeout=0.03)["engine_closed"]
    finally:
        release.set()
    assert svc.close(timeout=3)["engine_closed"]
    assert svc.list_jobs()[0].status == "cancelled"
    assert svc.refresh_status()["running"] is False
    assert svc.refresher.read(ReadPolicy(freshness="wait")).status == "disabled"


def test_rest_and_mcp_refuse_work_with_shutdown_hint(tmp_path):
    app = create_app(GragConfig(db_path=tmp_path / "api.lbdb"))
    with TestClient(app) as client:
        svc = app.state.service
        svc.close()
        health = client.get("/api/health").json()
        assert health["status"] == "shutting_down"
        assert health["shutdown"]["engine_closed"]
        result = client.post("/api/query", json={"cypher": "RETURN 1"})
        assert result.status_code == 503 and result.json()["hint"]
        assert "shutting down" in mcp_server.cypher_query(svc, "RETURN 1")


@pytest.mark.parametrize("state", ["closed", "draining", "error"])
def test_daemon_registration_is_retained_until_engines_close(
    tmp_path, monkeypatch, capsys, state
):
    from types import SimpleNamespace

    import uvicorn

    from grag import admin, cli
    from grag.api import main

    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / "home")
    db = tmp_path / "daemon.lbdb"
    app = SimpleNamespace(
        state=SimpleNamespace(
            shutdown_results={
                str(db): {"engine_closed": state == "closed", "state": state},
            }
        )
    )
    monkeypatch.setattr(main, "create_app", lambda cfg: app)
    monkeypatch.setattr(
        uvicorn, "Server", lambda cfg: SimpleNamespace(run=lambda: None)
    )
    result = cli.main(["--db", str(db), "serve"])
    assert result == (0 if state == "closed" else 1)
    assert admin.pidfile_path(db).exists() == (state != "closed")
    if state != "closed":
        assert "keeping the server registration" in capsys.readouterr().err


def test_process_exit_waits_for_a_late_embedding_worker(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    code = textwrap.dedent("""
        import sys, threading, time
        from pathlib import Path
        from grag.config import GragConfig
        from grag.embedworker import EmbedWorker
        from grag.service import GragService
        root = Path(sys.argv[1])
        svc = GragService(GragConfig(db_path=root / "process.lbdb"))
        entered = threading.Event()
        def blocked():
            entered.set()
            deadline = time.monotonic() + 8
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (root / "release").exists()
            assert svc.engine.execute("RETURN 42").rows == [[42]]
            (root / "worker-finished").write_text("ok")
            return 0
        worker = EmbedWorker(svc.engine, svc.config)
        worker.drain_once = blocked
        svc.embed_worker = worker
        worker.start()
        assert entered.wait(3)
        assert svc.close(timeout=0.03)["state"] == "draining"
        print("main returned while draining", flush=True)
    """)
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "GRAG_EMBED_PROVIDER": "",
    }
    process = subprocess.Popen(  # noqa: S603 — fixed test program and pytest temporary path
        [sys.executable, "-c", code, str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "main returned while draining"
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.15)
        (tmp_path / "release").touch()
        _, err = process.communicate(timeout=5)
        assert process.returncode == 0, err
        assert (tmp_path / "worker-finished").read_text() == "ok"
    finally:
        (tmp_path / "release").touch()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_streaming_export_refuses_native_reads_after_shutdown(tmp_path, monkeypatch):
    import asyncio

    from starlette.requests import Request

    app = create_app(GragConfig(db_path=tmp_path / "export.lbdb"))
    svc = app.state.service
    endpoint = next(
        r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/export"
    )
    request = Request(
        {"type": "http", "path": "/api/export", "query_string": b"", "headers": []}
    )

    async def stream():
        response = endpoint(request, ReadPolicy())
        first = await response.body_iterator.__anext__()
        assert '"type": "grag_export"' in first
        assert svc.close(timeout=3)["engine_closed"]
        monkeypatch.setattr(
            svc.engine, "execute", lambda *a: pytest.fail("native read after close")
        )
        with pytest.raises(ShutdownError):
            await response.body_iterator.__anext__()

    try:
        asyncio.run(stream())
    finally:
        app.state.registry.close()


def test_cancelled_queued_verification_is_not_reported_running(svc):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(5)
        return {}

    svc.jobs.submit("hold", hold, {})
    assert entered.wait(2)
    svc.enable_auto_refresh()
    svc.read_freshness()
    job_id = svc.refresh_status()["job_id"]
    assert svc.get_job(job_id).status == "queued"
    try:
        assert not svc.close(timeout=0.03)["engine_closed"]
        assert svc.get_job(job_id).status == "cancelled"
        assert svc.refresh_status()["running"] is False
    finally:
        release.set()
    assert svc.close(timeout=3)["engine_closed"]
