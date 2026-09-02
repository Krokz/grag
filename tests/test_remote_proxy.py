"""Remote-server mode: the proxy bridges stdio to a cloud-hosted grag server."""

from __future__ import annotations

import asyncio
import json

import pytest

from grag import cli, proxy
from grag.project import plan_claude_md_op, plan_mcp_ops

# --- URL validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://grag.example.com", "https://grag.example.com"),
        ("https://grag.example.com/", "https://grag.example.com"),
        ("https://grag.example.com:8443", "https://grag.example.com:8443"),
        ("http://127.0.0.1:47832", "http://127.0.0.1:47832"),
        ("http://localhost:47832/", "http://localhost:47832"),
    ],
)
def test_validate_server_url_normalises_origin(url, expected):
    assert proxy.validate_server_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "grag.example.com",
        "ftp://grag.example.com",
        "https://grag.example.com/mcp",
        "https://grag.example.com/?db=x",
        "https://grag.example.com/#frag",
        "https://",
    ],
)
def test_validate_server_url_rejects_malformed(url):
    with pytest.raises(SystemExit):
        proxy.validate_server_url(url)


def test_validate_server_url_refuses_plain_http_off_loopback():
    with pytest.raises(SystemExit, match="clear text"):
        proxy.validate_server_url("http://grag.internal:47832")


def test_validate_server_url_allows_plain_http_when_opted_in(capsys):
    assert (
        proxy.validate_server_url("http://grag.internal:47832", allow_insecure=True)
        == "http://grag.internal:47832"
    )
    assert "WARNING" in capsys.readouterr().err


# --- readiness + identity pinning -------------------------------------------------


def test_ensure_remote_server_waits_then_pins_identity(monkeypatch):
    probes = iter(
        [
            (False, None, None, None),
            (True, "db-A", True, "/mcp"),
        ]
    )
    calls = []

    async def probe(url, **kwargs):
        calls.append((url, kwargs))
        return next(probes)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    pinned: dict = {}
    path = asyncio.run(
        proxy._ensure_remote_server("https://grag.example.com", pinned)
    )
    assert path == "/mcp"
    assert pinned == {"database_id": "db-A"}
    # Remote probes go to the origin's health route and honour the environment
    # (corporate proxies / CA bundles), unlike loopback probes.
    assert calls[0][0] == "https://grag.example.com/api/health"
    assert calls[0][1]["trust_env"] is True


def test_ensure_remote_server_refuses_identity_change_on_heal(monkeypatch):
    async def probe(url, **kwargs):
        return True, "db-B", True, "/mcp"

    monkeypatch.setattr(proxy, "_probe_server", probe)

    with pytest.raises(SystemExit, match="different database"):
        asyncio.run(
            proxy._ensure_remote_server(
                "https://grag.example.com", {"database_id": "db-A"}
            )
        )


def test_ensure_remote_server_never_spawns_and_times_out(monkeypatch):
    from grag import admin

    async def probe(url, **kwargs):
        return False, None, None, None

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        admin.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("remote mode must never spawn a daemon"),
    )

    with pytest.raises(ConnectionError, match="did not become ready"):
        asyncio.run(
            proxy._ensure_remote_server(
                "https://grag.example.com", {}, wait_seconds=0.0
            )
        )


def test_ensure_remote_server_rejects_mcp_disabled(monkeypatch):
    async def probe(url, **kwargs):
        return True, "db-A", False, None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    with pytest.raises(SystemExit, match="MCP endpoint is disabled"):
        asyncio.run(proxy._ensure_remote_server("https://grag.example.com", {}))


# --- run_remote_proxy wiring ---------------------------------------------------------


def test_run_remote_proxy_sends_bearer_and_db_header(monkeypatch):
    from contextlib import asynccontextmanager

    import httpx2
    from mcp.client import streamable_http as streamable_http_module
    from mcp.server import stdio as stdio_module

    captured = {}

    async def ensure_remote(origin, pinned, **kwargs):
        captured["origin"] = origin
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
        raise RuntimeError("transport reached")
        yield  # pragma: no cover

    monkeypatch.setattr(proxy, "_ensure_remote_server", ensure_remote)
    monkeypatch.setattr(httpx2, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(stdio_module, "stdio_server", stdio_server)
    monkeypatch.setattr(
        streamable_http_module, "streamable_http_client", streamable_http_client
    )
    monkeypatch.setattr(proxy, "_MAX_SESSION_RESTARTS", 0)

    with pytest.raises(RuntimeError, match="transport reached"):
        asyncio.run(
            proxy.run_remote_proxy(
                "https://grag.example.com/",
                api_token="team-secret",  # noqa: S106 — test fixture
                db_name="algo4",
            )
        )

    assert captured["origin"] == "https://grag.example.com"
    assert captured["headers"] == {
        "Authorization": "Bearer team-secret",
        "x-grag-db": "algo4",
    }
    # Never follow a redirect with the bearer token; honour proxy/CA env vars.
    assert captured["client_kwargs"]["follow_redirects"] is False
    assert captured["client_kwargs"]["trust_env"] is True
    assert captured["url"] == "https://grag.example.com/agent-mcp/"


# --- CLI routing --------------------------------------------------------------------


def test_cli_routes_server_url_flag_to_remote_proxy(monkeypatch):
    calls = []

    async def run_remote_proxy(server_url, *, api_token=None, db_name=None, allow_insecure=False):
        calls.append((server_url, api_token, db_name, allow_insecure))

    monkeypatch.setenv("GRAG_API_TOKEN", "team-secret")
    monkeypatch.delenv("GRAG_SERVER_URL", raising=False)
    monkeypatch.setattr(proxy, "run_remote_proxy", run_remote_proxy)
    monkeypatch.setattr(
        proxy, "run_proxy", lambda *a, **k: pytest.fail("local proxy must not run")
    )

    assert (
        cli.main(
            [
                "mcp",
                "--server-url",
                "https://grag.example.com",
                "--server-db",
                "algo4",
                "--auto-serve",
            ]
        )
        == 0
    )
    assert calls == [("https://grag.example.com", "team-secret", "algo4", False)]


def test_cli_routes_server_url_env_to_remote_proxy(monkeypatch):
    calls = []

    async def run_remote_proxy(server_url, **kwargs):
        calls.append((server_url, kwargs))

    monkeypatch.setenv("GRAG_SERVER_URL", "https://grag.example.com")
    monkeypatch.setenv("GRAG_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.delenv("GRAG_API_TOKEN", raising=False)
    monkeypatch.setattr(proxy, "run_remote_proxy", run_remote_proxy)

    assert cli.main(["mcp"]) == 0
    assert calls[0][0] == "https://grag.example.com"
    assert calls[0][1]["allow_insecure"] is True


# --- grag init --server-url ----------------------------------------------------------


def test_plan_mcp_ops_remote_stdio_references_token(tmp_path):
    ops = plan_mcp_ops(
        ["claude"],
        tmp_path,
        tmp_path / "unused.lbdb",
        server_url="https://grag.example.com",
        server_db="algo4",
    )
    entry = json.loads(ops[0].content)["mcpServers"]["grag"]
    assert entry["args"] == [
        "mcp",
        "--server-url",
        "https://grag.example.com",
        "--server-db",
        "algo4",
    ]
    assert entry["env"] == {"GRAG_API_TOKEN": "${GRAG_API_TOKEN}"}
    assert "unused.lbdb" not in ops[0].content


def test_plan_mcp_ops_remote_url_transport(tmp_path):
    ops = plan_mcp_ops(
        ["cursor"],
        tmp_path,
        tmp_path / "unused.lbdb",
        stdio=False,
        server_url="https://grag.example.com",
    )
    entry = json.loads(ops[0].content)["mcpServers"]["grag"]
    assert entry == {
        "type": "http",
        "url": "https://grag.example.com/mcp/",
        "headers": {"Authorization": "Bearer ${GRAG_API_TOKEN}"},
    }


def test_claude_md_block_documents_remote_server(tmp_path):
    op = plan_claude_md_op(
        tmp_path, tmp_path / "unused.lbdb", server_url="https://grag.example.com"
    )
    assert "https://grag.example.com" in op.content
    assert "unused.lbdb" not in op.content
    assert "search_knowledge" in op.content


def test_cli_init_server_url_writes_remote_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("grag.project.detect_clients", lambda root: ["claude"])
    assert (
        cli.main(
            [
                "init",
                "--server-url",
                "https://grag.example.com",
                "--no-skill",
            ]
        )
        == 0
    )
    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["grag"]
    assert entry["args"][:3] == ["mcp", "--server-url", "https://grag.example.com"]
    assert "https://grag.example.com" in (tmp_path / "CLAUDE.md").read_text()


def test_cli_init_rejects_bad_server_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--server-url", "grag.example.com/mcp"]) == 2
    assert "server-url" in capsys.readouterr().err
    assert not (tmp_path / ".mcp.json").exists()
