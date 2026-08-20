"""Tests for grag.admin (status / stop / doctor plumbing) and derived ports."""

from __future__ import annotations

import json
import os

import pytest

from grag import admin
from grag.config import GragConfig, derive_port


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.grag during tests."""
    monkeypatch.setattr(admin, "GRAG_HOME", tmp_path / ".grag")


# ---------------------------------------------------------------------------
# derive_port
# ---------------------------------------------------------------------------


def test_derive_port_is_deterministic_and_in_range(tmp_path):
    a = tmp_path / "alpha.lbdb"
    assert derive_port(a) == derive_port(a)
    assert 41000 <= derive_port(a) <= 49151


def test_derive_port_differs_per_database(tmp_path):
    ports = {derive_port(tmp_path / f"project-{i}.lbdb") for i in range(50)}
    # 50 projects should essentially never all collide; require real spread.
    assert len(ports) > 45


# ---------------------------------------------------------------------------
# pidfile
# ---------------------------------------------------------------------------


def test_pidfile_roundtrip(tmp_path):
    db = tmp_path / "kb.lbdb"
    admin.write_pidfile(db, 41234)
    info = admin.read_pidfile(db)
    assert info is not None
    assert info["pid"] == os.getpid()
    assert info["port"] == 41234
    assert info["db"] == str(db.resolve())
    admin.remove_pidfile(db)
    assert admin.read_pidfile(db) is None


def test_read_pidfile_tolerates_garbage(tmp_path):
    db = tmp_path / "kb.lbdb"
    admin.run_dir().mkdir(parents=True, exist_ok=True)
    admin.pidfile_path(db).write_text("not json")
    assert admin.read_pidfile(db) is None


def test_open_daemon_log_creates_log_dir(tmp_path):
    db = tmp_path / "kb.lbdb"
    fd = admin.open_daemon_log(db)
    assert isinstance(fd, int) and fd >= 0
    os.close(fd)
    assert admin.log_path(db).exists()


# ---------------------------------------------------------------------------
# status / stop
# ---------------------------------------------------------------------------


def test_status_reports_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "probe_health", lambda port: None)
    cfg = GragConfig(db_path=tmp_path / "kb.lbdb")
    text = "\n".join(admin.status_lines(cfg))
    assert "not running" in text
    assert "FTS-only" in text  # no embedder configured


def test_status_reports_running_server(tmp_path, monkeypatch):
    from grag.config import database_identity

    db = tmp_path / "kb.lbdb"
    identity = database_identity(db)
    port_used = derive_port(db)

    def fake_probe(port):
        if port == port_used:
            return {"status": "ok", "version": "9.9.9", "database_id": identity}
        return None

    monkeypatch.setattr(admin, "probe_health", fake_probe)
    text = "\n".join(admin.status_lines(GragConfig(db_path=db)))
    assert f"http://127.0.0.1:{port_used}/" in text
    assert "9.9.9" in text


def test_status_cleans_stale_pidfile(tmp_path, monkeypatch):
    db = tmp_path / "kb.lbdb"
    admin.run_dir().mkdir(parents=True, exist_ok=True)
    admin.pidfile_path(db).write_text(
        json.dumps({"pid": 99999999, "port": 41234, "db": str(db)})
    )
    monkeypatch.setattr(admin, "probe_health", lambda port: None)
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    "\n".join(admin.status_lines(GragConfig(db_path=db)))
    assert admin.read_pidfile(db) is None


def test_stop_without_pidfile_reports_nothing_to_stop(tmp_path):
    assert "nothing to stop" in admin.stop_server(tmp_path / "kb.lbdb").lower()


def test_stop_with_dead_pid_cleans_up(tmp_path, monkeypatch):
    db = tmp_path / "kb.lbdb"
    admin.write_pidfile(db, 41234)
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    message = admin.stop_server(db)
    assert "stale" in message.lower()
    assert admin.read_pidfile(db) is None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_install_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "probe_health", lambda port: None)
    cfg = GragConfig(db_path=tmp_path / "kb.lbdb")
    text = "\n".join(admin.doctor_lines(cfg))
    assert "core engine (ladybug)" in text
    assert "installed" in text
    assert "server:" in text


def test_repo_staleness_lines():
    rows = [
        {"path": "/some/repo", "git_commit": None, "ingested_at": None},
    ]
    lines = admin._repo_staleness_lines(rows)
    assert len(lines) == 1
    assert "no git commit recorded" in lines[0]
