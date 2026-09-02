"""Background ingest jobs: service API, REST endpoints, MCP tools."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from grag.api.main import create_app
from grag.config import EmbedderConfig, GragConfig
from grag.core.types import CodeIngestRequest, IngestDocument, IngestRequest
from grag.retrieval import vectors
from grag.service import GragService
from test_vectors import FAKE_DIM, FakeEmbedder


def _wait_done(svc: GragService, job_id: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = svc.get_job(job_id)
        if job.status in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_submit_ingest_code_runs_in_background(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    svc = GragService(GragConfig(db_path=tmp_path / "jobs.lbdb"))
    try:
        job = svc.submit_ingest_code(CodeIngestRequest(paths=[str(pkg)]))
        assert job.status == "queued" and job.kind == "ingest_code"
        done = _wait_done(svc, job.id)
        assert done.status == "done"
        assert done.result["functions"] == 1
        assert done.started_at and done.finished_at
        assert svc.list_jobs()[0].id == job.id
    finally:
        svc.close()


def test_failed_job_records_error(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "jobs.lbdb"))
    try:
        job = svc.submit_ingest(
            IngestRequest(documents=[IngestDocument(text="x")], label="bad label!")
        )
        done = _wait_done(svc, job.id)
        assert done.status == "failed"
        assert done.error
    finally:
        svc.close()


def test_jobs_rest_endpoints(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    app = create_app(GragConfig(db_path=tmp_path / "api-jobs.lbdb"))
    with TestClient(app) as client:
        r = client.post("/api/jobs/ingest/code", json={"paths": [str(pkg)]})
        assert r.status_code == 202
        job_id = r.json()["id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            body = client.get(f"/api/jobs/{job_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert body["status"] == "done"
        assert body["result"]["functions"] == 1
        assert client.get("/api/jobs").json()["jobs"][0]["id"] == job_id
        assert client.get("/api/jobs/nope").status_code == 404


def test_mcp_ingest_code_background_and_job_status(tmp_path):
    from grag.mcp_server import server as mcp_server

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    svc = GragService(GragConfig(db_path=tmp_path / "mcp-jobs.lbdb"))
    try:
        queued = json.loads(mcp_server.ingest_code(svc, [str(pkg)], background=True))
        assert queued["status"] == "queued"
        _wait_done(svc, queued["id"])
        polled = json.loads(mcp_server.job_status(svc, queued["id"]))
        assert polled["status"] == "done"
        assert polled["result"]["modules"] == 1
        assert mcp_server.job_status(svc, "missing").startswith("ERROR:")
    finally:
        svc.close()


def test_health_reports_embedding_worker(tmp_path, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(
        vectors,
        "get_embedder",
        lambda config: fake if config.embedder is not None else None,
    )
    cfg = GragConfig(
        db_path=tmp_path / "health.lbdb",
        embedder=EmbedderConfig(provider="fastembed", model="fake", dim=FAKE_DIM),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["embedding"]["running"] is True
    app.state.registry.close()
    # FTS-only servers report no worker at all.
    app2 = create_app(GragConfig(db_path=tmp_path / "plain.lbdb"))
    with TestClient(app2) as client:
        assert client.get("/api/health").json()["embedding"] is None
