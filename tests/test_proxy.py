"""Safety checks for the stdio-to-HTTP auto-serve proxy."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest

from grag import admin, cli, proxy
from grag.config import database_identity


def test_probe_server_real_import_against_dead_port():
    """Run the real probe (no mocks) so its HTTP-client import is exercised.

    Every other test here monkeypatches _probe_server, which would hide an
    ImportError inside it until the first real auto-serve connection.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Nothing is listening on `port` anymore: probe must report unreachable.
    reachable, identity, mcp_enabled, mcp_path = asyncio.run(
        proxy._probe_server(f"http://127.0.0.1:{port}/api/health")
    )
    assert reachable is False
    assert identity is None
    assert mcp_enabled is None
    assert mcp_path is None


def test_probe_server_reads_custom_mcp_path(monkeypatch):
    import httpx2

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "database_id": "db-id",
                "mcp_enabled": True,
                "mcp_path": "/agent-mcp",
            }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"trust_env": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, timeout):
            assert url == "http://127.0.0.1:8471/api/health"
            assert timeout == 2.0
            return Response()

    monkeypatch.setattr(httpx2, "AsyncClient", Client)

    assert asyncio.run(proxy._probe_server("http://127.0.0.1:8471/api/health")) == (
        True,
        "db-id",
        True,
        "/agent-mcp",
    )


def test_ensure_server_reuses_only_matching_database(tmp_path, monkeypatch):
    db = tmp_path / "project.lbdb"
    calls = []

    async def probe(_url):
        return True, database_identity(db), True, "/agent-mcp"

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(admin.subprocess, "Popen", lambda *a, **kw: calls.append(a))

    mcp_path = asyncio.run(
        proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health")
    )
    assert calls == []
    assert mcp_path == "/agent-mcp"


def test_ensure_server_refuses_different_database(tmp_path, monkeypatch):
    wanted = tmp_path / "wanted.lbdb"
    other = tmp_path / "other.lbdb"

    async def probe(_url):
        return True, database_identity(other), True, "/mcp"

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(
        admin.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("must not start over an occupied port"),
    )

    with pytest.raises(SystemExit, match="different or unidentified database"):
        asyncio.run(
            proxy._ensure_server(wanted, 8471, "http://127.0.0.1:8471/api/health")
        )


def test_ensure_server_refuses_legacy_unidentified_server(tmp_path, monkeypatch):
    async def probe(_url):
        return True, None, None, None

    monkeypatch.setattr(proxy, "_probe_server", probe)

    with pytest.raises(SystemExit, match="different or unidentified database"):
        asyncio.run(
            proxy._ensure_server(
                tmp_path / "wanted.lbdb",
                8471,
                "http://127.0.0.1:8471/api/health",
            )
        )


def test_ensure_server_refuses_matching_rest_only_server(tmp_path, monkeypatch):
    db = tmp_path / "rest-only.lbdb"

    async def probe(_url):
        return True, database_identity(db), False, None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(
        admin.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("REST-only server must not be reused"),
    )

    with pytest.raises(SystemExit, match="MCP endpoint is disabled"):
        asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))


@pytest.mark.parametrize(
    "mcp_path",
    [
        None,
        "mcp",
        "/",
        "/api/mcp",
        "/assets",
        "/assets/app.js",
        "//evil.example/mcp",
        "/mcp?target=evil",
        "/mcp/\ud800",
    ],
)
def test_ensure_server_refuses_invalid_reported_mcp_path(
    mcp_path, tmp_path, monkeypatch
):
    db = tmp_path / "project.lbdb"

    async def probe(_url):
        return True, database_identity(db), True, mcp_path

    monkeypatch.setattr(proxy, "_probe_server", probe)

    with pytest.raises(SystemExit, match="invalid MCP path"):
        asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))


def test_ensure_server_starts_and_waits_for_matching_database(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")

    db = tmp_path / "project.lbdb"
    probes = iter(
        [
            (False, None, None, None),
            (False, None, None, None),
            (True, database_identity(db), True, "/agent-mcp/"),
        ]
    )
    popen_calls = []

    async def probe(_url):
        return next(probes)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(
        admin.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw))
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    mcp_path = asyncio.run(
        proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health")
    )
    assert len(popen_calls) == 1
    argv = popen_calls[0][0][0]
    assert str(db.resolve()) in argv
    assert mcp_path == "/agent-mcp"


def test_auto_serve_reaps_confirmed_dead_pidfile_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    db = tmp_path / "project.lbdb"
    admin.write_pidfile(db, 8471)
    path = admin.pidfile_path(db)
    registration = json.loads(path.read_text(encoding="utf-8"))
    registration["pid"] = 999_999_999
    path.write_text(json.dumps(registration), encoding="utf-8")
    probes = iter(
        [(False, None, None, None), (True, database_identity(db), True, "/mcp")]
    )

    async def probe(_url):
        return next(probes)

    async def no_sleep(_seconds):
        return None

    popen_calls = []
    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        admin.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append(args)
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))

    assert len(popen_calls) == 1
    assert not path.exists()


def test_auto_serve_attaches_to_matching_concurrent_start(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    db = tmp_path / "project.lbdb"
    assert admin.write_pidfile(db, 8471, with_mcp=True)
    probes = iter(
        [(False, None, None, None), (True, database_identity(db), True, "/mcp")]
    )

    async def probe(_url):
        return next(probes)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))

    assert admin.pidfile_path(db).exists()
    assert "another process is starting" in capsys.readouterr().err


def test_cli_passes_api_token_to_auto_serve(monkeypatch):
    calls = []

    async def run_proxy(db_path, port, *, api_token=None):
        calls.append((db_path, port, api_token))

    monkeypatch.setenv("GRAG_API_TOKEN", "proxy-secret")
    monkeypatch.delenv("GRAG_DB_DIR", raising=False)
    monkeypatch.setattr(proxy, "run_proxy", run_proxy)

    assert cli.main(["mcp", "--auto-serve", "--port", "42001"]) == 0
    assert calls and calls[0][1:] == (42001, "proxy-secret")


def test_run_proxy_passes_bearer_client_to_mcp_transport(tmp_path, monkeypatch):
    import httpx2
    from mcp.client import streamable_http as streamable_http_module
    from mcp.server import stdio as stdio_module

    captured = {}

    async def ensure_server(*args, **kwargs):
        return "/agent-mcp"

    class FakeHttpClient:
        def __init__(self, *, headers=None, **kwargs):
            captured["headers"] = headers
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    @asynccontextmanager
    async def stdio_server():
        yield object(), object()

    @asynccontextmanager
    async def streamable_http_client(url, *, http_client):
        captured["url"] = url
        captured["client"] = http_client
        raise RuntimeError("transport reached")
        yield  # pragma: no cover

    monkeypatch.setattr(proxy, "_ensure_server", ensure_server)
    monkeypatch.setattr(httpx2, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(stdio_module, "stdio_server", stdio_server)
    monkeypatch.setattr(
        streamable_http_module, "streamable_http_client", streamable_http_client
    )

    with pytest.raises(RuntimeError, match="transport reached"):
        asyncio.run(
            proxy.run_proxy(
                tmp_path / "project.lbdb",
                42001,
                api_token="proxy-secret",  # noqa: S106 — test fixture
            )
        )

    assert captured["headers"] == {"Authorization": "Bearer proxy-secret"}
    assert captured["client_kwargs"]["follow_redirects"] is True
    assert captured["client_kwargs"]["trust_env"] is False
    assert captured["client_kwargs"]["timeout"].read == 300.0
    assert captured["url"] == "http://127.0.0.1:42001/agent-mcp/"
    assert captured["client"] is not None


@pytest.mark.parametrize("from_environment", [False, True])
def test_auto_serve_rejects_multi_db_mode(
    from_environment, tmp_path, monkeypatch, capsys
):
    db_dir = tmp_path / "dbs"
    argv = ["mcp", "--auto-serve"]
    if from_environment:
        monkeypatch.setenv("GRAG_DB_DIR", str(db_dir))
    else:
        argv = ["--db-dir", str(db_dir), *argv]
    monkeypatch.setattr(
        proxy,
        "run_proxy",
        lambda *args: pytest.fail("invalid multi-db auto-serve must not run"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "--db-dir cannot be used" in error
    assert "serve --with-mcp" in error
