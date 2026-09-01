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

    async def no_sleep(_seconds):
        return None

    # A crashed server leaves its pidfile behind; without one the heal loop
    # treats the loss as a deliberate `grag stop` and exits instead.
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    assert admin.write_pidfile(tmp_path / "project.lbdb", 42001)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
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


def _session_msg(message):
    from mcp.shared.message import SessionMessage

    return SessionMessage(message=message)


class _FakeHttpClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def test_run_proxy_reconnects_and_replays_initialize(tmp_path, monkeypatch):
    """A mid-session server crash heals in-band: the proxy re-ensures the
    server, reconnects, replays the captured initialize handshake under a
    proxy-internal id, and resumes forwarding — the client never re-initializes.
    """
    import anyio
    import httpx2
    from mcp.client import streamable_http as streamable_http_module
    from mcp.server import stdio as stdio_module
    from mcp.types import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

    # stdio ends: test acts as the MCP client.
    stdio_client_send, stdio_read = anyio.create_memory_object_stream(100)
    stdio_write, stdio_client_recv = anyio.create_memory_object_stream(100)
    # Two upstream sessions: session 1 dies mid-stream, session 2 is the heal.
    # upstream_respond_N feeds the proxy's http_read; upstream_requests_N
    # drains the proxy's http_write.
    upstream_respond_1, http_read_1 = anyio.create_memory_object_stream(100)
    http_write_1, upstream_requests_1 = anyio.create_memory_object_stream(100)
    upstream_respond_2, http_read_2 = anyio.create_memory_object_stream(100)
    http_write_2, upstream_requests_2 = anyio.create_memory_object_stream(100)
    sessions = iter(
        [
            (http_read_1, http_write_1),
            (http_read_2, http_write_2),
        ]
    )

    async def ensure_server(*args, **kwargs):
        return "/mcp"

    @asynccontextmanager
    async def fake_stdio():
        yield stdio_read, stdio_write

    @asynccontextmanager
    async def fake_http(url, *, http_client):
        yield next(sessions)

    async def no_sleep(_seconds):
        return None

    # A crashed server leaves its pidfile behind; the heal loop keys on that.
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    db = tmp_path / "project.lbdb"
    assert admin.write_pidfile(db, 42001)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(proxy, "_ensure_server", ensure_server)
    monkeypatch.setattr(httpx2, "AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(stdio_module, "stdio_server", fake_stdio)
    monkeypatch.setattr(streamable_http_module, "streamable_http_client", fake_http)

    async def client_and_upstream():
        init_params = {"protocolVersion": "2025-03-26", "capabilities": {}}
        # Session 1: client initializes through the pipe.
        await stdio_client_send.send(
            _session_msg(
                JSONRPCRequest(
                    jsonrpc="2.0", id=1, method="initialize", params=init_params
                )
            )
        )
        forwarded = await upstream_requests_1.receive()
        assert forwarded.message.method == "initialize"
        await upstream_respond_1.send(
            _session_msg(JSONRPCResponse(jsonrpc="2.0", id=1, result={"ok": 1}))
        )
        assert (await stdio_client_recv.receive()).message.id == 1
        await stdio_client_send.send(
            _session_msg(
                JSONRPCNotification(
                    jsonrpc="2.0", method="notifications/initialized"
                )
            )
        )
        await upstream_requests_1.receive()

        # Crash: the upstream read stream closes.
        await upstream_respond_1.aclose()

        # Session 2: proxy must replay initialize under its own id, then the
        # initialized notification, before any client traffic is forwarded.
        replayed = await upstream_requests_2.receive()
        assert replayed.message.method == "initialize"
        assert replayed.message.id == "grag-proxy-reinit"
        assert replayed.message.params == init_params
        await upstream_respond_2.send(
            _session_msg(
                JSONRPCResponse(
                    jsonrpc="2.0", id="grag-proxy-reinit", result={"ok": 1}
                )
            )
        )
        replayed_note = await upstream_requests_2.receive()
        assert replayed_note.message.method == "notifications/initialized"

        # A post-crash tool call flows to the fresh session.
        await stdio_client_send.send(
            _session_msg(
                JSONRPCRequest(
                    jsonrpc="2.0", id=2, method="tools/call", params={"name": "x"}
                )
            )
        )
        healed = await upstream_requests_2.receive()
        assert healed.message.method == "tools/call"

        # Client disconnects: proxy shuts down normally.
        await stdio_client_send.aclose()

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(client_and_upstream)
            tg.start_soon(
                proxy.run_proxy, tmp_path / "project.lbdb", 42001
            )

    asyncio.run(run())


def test_run_proxy_gives_up_after_max_restarts(tmp_path, monkeypatch):
    """A server that never comes up must not retry-loop forever."""
    import httpx2
    from mcp.client import streamable_http as streamable_http_module
    from mcp.server import stdio as stdio_module

    async def ensure_server(*args, **kwargs):
        return "/mcp"

    @asynccontextmanager
    async def fake_stdio():
        yield object(), object()

    @asynccontextmanager
    async def fake_http(url, *, http_client):
        raise ConnectionRefusedError("upstream never comes up")
        yield  # pragma: no cover

    async def no_sleep(_seconds):
        return None

    # A crashed server leaves its pidfile behind; without one the heal loop
    # treats the loss as a deliberate `grag stop` and exits instead.
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    assert admin.write_pidfile(tmp_path / "project.lbdb", 42001)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(proxy, "_MAX_SESSION_RESTARTS", 2)
    monkeypatch.setattr(proxy, "_ensure_server", ensure_server)
    monkeypatch.setattr(httpx2, "AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(stdio_module, "stdio_server", fake_stdio)
    monkeypatch.setattr(streamable_http_module, "streamable_http_client", fake_http)

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(proxy.run_proxy(tmp_path / "project.lbdb", 42001))


def test_run_proxy_respects_deliberate_stop(tmp_path, monkeypatch, capsys):
    """`grag stop` removes the pidfile — the heal loop must NOT resurrect a
    deliberately stopped server; the proxy exits with an explanation instead."""
    import anyio
    import httpx2
    from mcp.client import streamable_http as streamable_http_module
    from mcp.server import stdio as stdio_module

    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")
    db = tmp_path / "project.lbdb"

    _stdio_client_send, stdio_read = anyio.create_memory_object_stream(100)
    stdio_write, _stdio_client_recv = anyio.create_memory_object_stream(100)
    upstream_respond, http_read = anyio.create_memory_object_stream(100)
    http_write, _upstream_requests = anyio.create_memory_object_stream(100)

    ensure_calls = []

    async def ensure_server(*args, **kwargs):
        ensure_calls.append(args)
        return "/mcp"

    @asynccontextmanager
    async def fake_stdio():
        yield stdio_read, stdio_write

    @asynccontextmanager
    async def fake_http(url, *, http_client):
        yield http_read, http_write

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(proxy, "_ensure_server", ensure_server)
    monkeypatch.setattr(httpx2, "AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(stdio_module, "stdio_server", fake_stdio)
    monkeypatch.setattr(streamable_http_module, "streamable_http_client", fake_http)

    async def stop_server_mid_session():
        # Simulate `grag stop`: the upstream dies AND the pidfile is gone
        # (graceful shutdown removes it; a crash would leave it behind).
        await upstream_respond.aclose()

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(stop_server_mid_session)
            tg.start_soon(proxy.run_proxy, db, 42001)

    asyncio.run(run())  # exits cleanly, not with a SystemExit traceback

    # Startup ensured once; the heal path must not try to respawn.
    assert len(ensure_calls) == 1
    assert "stopped deliberately" in capsys.readouterr().err


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
