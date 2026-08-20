"""Safety checks for the stdio-to-HTTP auto-serve proxy."""

from __future__ import annotations

import asyncio
import socket

import pytest

from grag import proxy
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
    reachable, identity = asyncio.run(
        proxy._probe_server(f"http://127.0.0.1:{port}/api/health")
    )
    assert reachable is False
    assert identity is None


def test_ensure_server_reuses_only_matching_database(tmp_path, monkeypatch):
    db = tmp_path / "project.lbdb"
    calls = []

    async def probe(_url):
        return True, database_identity(db)

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(proxy.subprocess, "Popen", lambda *a, **kw: calls.append(a))

    asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))
    assert calls == []


def test_ensure_server_refuses_different_database(tmp_path, monkeypatch):
    wanted = tmp_path / "wanted.lbdb"
    other = tmp_path / "other.lbdb"

    async def probe(_url):
        return True, database_identity(other)

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(
        proxy.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("must not start over an occupied port"),
    )

    with pytest.raises(SystemExit, match="different or unidentified database"):
        asyncio.run(
            proxy._ensure_server(wanted, 8471, "http://127.0.0.1:8471/api/health")
        )


def test_ensure_server_refuses_legacy_unidentified_server(tmp_path, monkeypatch):
    async def probe(_url):
        return True, None

    monkeypatch.setattr(proxy, "_probe_server", probe)

    with pytest.raises(SystemExit, match="different or unidentified database"):
        asyncio.run(
            proxy._ensure_server(
                tmp_path / "wanted.lbdb",
                8471,
                "http://127.0.0.1:8471/api/health",
            )
        )


def test_ensure_server_starts_and_waits_for_matching_database(tmp_path, monkeypatch):
    from grag import admin

    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")

    db = tmp_path / "project.lbdb"
    probes = iter([(False, None), (False, None), (True, database_identity(db))])
    popen_calls = []

    async def probe(_url):
        return next(probes)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(proxy, "_probe_server", probe)
    monkeypatch.setattr(
        proxy.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw))
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(proxy._ensure_server(db, 8471, "http://127.0.0.1:8471/api/health"))
    assert len(popen_calls) == 1
    argv = popen_calls[0][0][0]
    assert str(db.resolve()) in argv
