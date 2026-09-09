"""Snapshot transport validates completion and releases spools on disconnect."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from grag.api.main import create_app
from grag.api.snapshot import SnapshotResponse
from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.errors import GragError
from grag.transfer import capture_snapshot, export_lines, read_archive
from grag.transfer_io import export_client


@pytest.mark.parametrize(
    "failure", ["truncated", "disconnect", "old_server", "utf8", "redirect"]
)
def test_remote_download_rejects_unverifiable_response(
    engine, tmp_path, monkeypatch, failure
):
    complete = ("\n".join(export_lines(engine)) + "\n").encode()
    real_client = httpx2.Client
    requests = []

    class Broken(httpx2.SyncByteStream):
        def __iter__(self):
            yield complete[:40]
            raise httpx2.ReadError("connection lost")

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/api/health":
            return httpx2.Response(
                200,
                json={
                    "status": "ok",
                    "database_id": "selected",
                    "capabilities": {
                        "snapshot_format": 1 if failure == "old_server" else 2
                    },
                },
            )
        if failure == "disconnect":
            return httpx2.Response(200, stream=Broken())
        if failure == "redirect":
            return httpx2.Response(
                302, headers={"Location": "https://another.example/api/export"}
            )
        return httpx2.Response(
            200,
            content=complete[:-10] if failure == "truncated" else complete + b"\xff",
        )

    monkeypatch.setattr(
        "grag.client.httpx2.Client",
        lambda **kw: real_client(transport=httpx2.MockTransport(respond), **kw),
    )
    target = tmp_path / "backup.jsonl"
    target.write_text("original")
    with (
        GraphClient(GragConfig(server_url="https://graph.example")) as client,
        pytest.raises(GragError),
    ):
        export_client(client, str(target))
    assert target.read_text() == "original"
    if failure == "old_server":
        assert "/api/export" not in requests
    assert not list(tmp_path.glob(".*.partial-*"))


def test_server_capture_error_returns_error_before_success_headers(tmp_path):
    app = create_app(
        GragConfig(db_path=tmp_path / "api.lbdb", buffer_pool_size=128 * 1024**2)
    )
    with TestClient(app) as client:
        app.state.service.engine.execute_write(
            "CREATE NODE TABLE Unsupported(id STRING PRIMARY KEY, list STRING[])"
        )
        response = client.get("/api/export")
        assert response.status_code >= 400
        assert "unsupported type" in response.text
        assert "Content-Disposition" not in response.headers


@pytest.mark.parametrize("when", ["headers", "body", "cancelled"])
def test_delivery_failure_always_closes_snapshot(engine, monkeypatch, when):
    import grag.api.snapshot as module

    closed = []

    @contextmanager
    def tracked(engine):
        with capture_snapshot(engine) as file:
            try:
                yield file
            finally:
                closed.append(True)

    monkeypatch.setattr(module, "capture_snapshot", tracked)
    response = SnapshotResponse.capture(engine, headers={})

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == (
            "http.response.start" if when == "headers" else "http.response.body"
        ):
            if when == "cancelled":
                raise asyncio.CancelledError()
            raise OSError("disconnected")

    async def run():
        with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )

    asyncio.run(run())
    response.close()
    assert closed == [True]


def test_multiple_databases_export_only_selected_graph(tmp_path):
    root = tmp_path / "graphs"
    root.mkdir()
    from grag.core.engine import Engine
    for name in ("one", "two"):
        with Engine(GragConfig(db_path=root / f"{name}.lbdb", buffer_pool_size=128 * 1024**2)):
            pass
    app = create_app(GragConfig(db_dir=root, buffer_pool_size=128 * 1024**2))
    with TestClient(app) as client:
        for name in ("one", "two"):
            headers = {"x-grag-db": name}
            assert (
                client.post(
                    "/api/schema/define",
                    headers=headers,
                    json={"node_tables": [{"name": "Note"}], "rel_tables": []},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/nodes/upsert",
                    headers=headers,
                    json={"nodes": [{"label": "Note", "key": name}]},
                ).status_code
                == 200
            )
        with read_archive(
            client.get("/api/export", headers={"x-grag-db": "two"}).text.splitlines()
        ) as archive:
            assert [
                r["key"] for r in archive.records() if r.get("label") == "Note"
            ] == ["two"]
