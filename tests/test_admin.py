"""Tests for grag.admin (status / stop / doctor plumbing) and derived ports."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from grag import admin, cli
from grag.config import GragConfig, database_identity, derive_port


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
    admin.write_pidfile(
        db,
        41234,
        host="::1",
        with_mcp=True,
        mcp_path="/agent-mcp",
        shutdown_token="stop-secret",  # noqa: S106 — test fixture
    )
    info = admin.read_pidfile(db)
    assert info is not None
    assert info["pid"] == os.getpid()
    assert info["port"] == 41234
    assert info["db"] == str(db.resolve())
    assert info["host"] == "::1"
    assert info["with_mcp"] is True
    assert info["mcp_path"] == "/agent-mcp"
    assert info["shutdown_token"] == "stop-secret"  # noqa: S105
    if os.name != "nt":
        assert admin.pidfile_path(db).stat().st_mode & 0o777 == 0o600
    admin.remove_pidfile(db)
    assert admin.read_pidfile(db) is None


def test_read_pidfile_tolerates_garbage(tmp_path):
    db = tmp_path / "kb.lbdb"
    admin.run_dir().mkdir(parents=True, exist_ok=True)
    admin.pidfile_path(db).write_text("not json")
    assert admin.read_pidfile(db) is None


def test_pidfile_claim_is_atomic_and_owner_removal_is_guarded(tmp_path):
    db = tmp_path / "kb.lbdb"
    assert admin.write_pidfile(db, 41234, shutdown_token="first") is True  # noqa: S106
    assert admin.write_pidfile(db, 41235, shutdown_token="second") is False  # noqa: S106
    info = admin.read_pidfile(db)
    assert info is not None and info["shutdown_token"] == "first"  # noqa: S105

    admin.remove_pidfile(
        db, owner_pid=os.getpid(), shutdown_token="wrong"  # noqa: S106
    )
    assert admin.pidfile_path(db).exists()
    admin.remove_pidfile(
        db, owner_pid=os.getpid(), shutdown_token="first"  # noqa: S106
    )
    assert not admin.pidfile_path(db).exists()


def test_registration_claim_and_removal_share_one_target_lock(tmp_path):
    db = tmp_path / "kb.lbdb"
    path = admin.pidfile_path(db)
    admin.run_dir().mkdir(parents=True)

    writer_started = threading.Event()
    writer_result = []

    def write() -> None:
        writer_started.set()
        writer_result.append(
            admin.write_pidfile(
                db,
                41234,
                shutdown_token="owner-token",  # noqa: S106 — test fixture
            )
        )

    with admin._registration_lock(path):
        writer = threading.Thread(target=write)
        writer.start()
        assert writer_started.wait(timeout=1)
        writer.join(timeout=0.05)
        assert writer.is_alive()
        assert not path.exists()
    writer.join(timeout=2)
    assert not writer.is_alive()
    assert writer_result == [True]

    remover_started = threading.Event()
    remover_result = []

    def remove() -> None:
        remover_started.set()
        remover_result.append(
            admin._remove_registration_if_matches(
                path,
                owner_pid=os.getpid(),
                shutdown_token="owner-token",  # noqa: S106 — test fixture
            )
        )

    with admin._registration_lock(path):
        remover = threading.Thread(target=remove)
        remover.start()
        assert remover_started.wait(timeout=1)
        remover.join(timeout=0.05)
        assert remover.is_alive()
        assert path.exists()
    remover.join(timeout=2)
    assert not remover.is_alive()
    assert remover_result == [True]
    assert not path.exists()


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
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)
    cfg = GragConfig(db_path=tmp_path / "kb.lbdb")
    text = "\n".join(admin.status_lines(cfg))
    assert "not running" in text
    assert "FTS-only" in text  # no embedder configured


def test_status_reports_running_server(tmp_path, monkeypatch):
    from grag.config import database_identity

    db = tmp_path / "kb.lbdb"
    identity = database_identity(db)
    port_used = derive_port(db)

    def fake_probe(port, host="127.0.0.1"):
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
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)
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


@pytest.mark.parametrize(
    ("bind_host", "origin"),
    [
        ("127.0.0.1", "http://127.0.0.1:41234"),
        ("0.0.0.0", "http://127.0.0.1:41234"),  # noqa: S104
        ("::", "http://[::1]:41234"),
        ("::1", "http://[::1]:41234"),
        ("192.0.2.10", "http://192.0.2.10:41234"),
    ],
)
def test_http_origin_reaches_recorded_bind_safely(bind_host, origin):
    assert admin._http_origin(bind_host, 41234) == origin


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("bad/path", 41234),
        ("bad\x00host", 41234),
        ("bad\ud800host", 41234),
        ("127.0.0.1", 0),
        ("::1", 65536),
    ],
)
def test_http_origin_rejects_malformed_registration_values(host, port):
    assert admin._http_origin(host, port) is None


def test_find_server_probes_recorded_host(tmp_path, monkeypatch):
    db = tmp_path / "ipv6.lbdb"
    admin.write_pidfile(db, 41234, host="::1")
    calls = []

    def probe(port, host="127.0.0.1"):
        calls.append((host, port))
        return {
            "status": "ok",
            "server_id": database_identity(db),
            "pid": os.getpid(),
            "mcp_enabled": False,
        }

    monkeypatch.setattr(admin, "probe_health", probe)
    info = admin.find_server(db)
    assert info is not None and info.host == "::1" and info.port == 41234
    assert calls == [("::1", 41234)]


def test_verified_stop_prefers_graceful_channel_over_signal(tmp_path, monkeypatch):
    db = tmp_path / "managed.lbdb"
    admin.write_pidfile(
        db, 41234, shutdown_token="stop-secret"  # noqa: S106 — test fixture
    )
    alive = {os.getpid(): True}
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "server_id": database_identity(db),
            "pid": os.getpid(),
        },
    )

    def graceful(server):
        alive[server.pid] = False
        return True

    monkeypatch.setattr(admin, "_request_graceful_shutdown", graceful)
    monkeypatch.setattr(
        admin.os,
        "kill",
        lambda *args: pytest.fail("managed stop must not use an OS signal"),
    )
    outcome = admin.stop_server_result(db)
    assert outcome.stopped is True
    assert not admin.pidfile_path(db).exists()


def test_verified_stop_reports_registration_cleanup_failure(tmp_path, monkeypatch):
    db = tmp_path / "managed.lbdb"
    admin.write_pidfile(
        db, 41234, shutdown_token="stop-secret"  # noqa: S106 — test fixture
    )
    alive = {os.getpid(): True}
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "server_id": database_identity(db),
            "pid": os.getpid(),
        },
    )

    def graceful(server):
        alive[server.pid] = False
        return True

    monkeypatch.setattr(admin, "_request_graceful_shutdown", graceful)
    monkeypatch.setattr(
        admin, "_remove_registration_if_matches", lambda *args, **kwargs: False
    )

    outcome = admin.stop_server_result(db)

    assert outcome.stopped is False
    assert "exited" in outcome.message
    assert "could not be safely removed" in outcome.message


def test_verified_stop_rechecks_ownership_before_signal_fallback(
    tmp_path, monkeypatch
):
    db = tmp_path / "managed.lbdb"
    admin.write_pidfile(db, 41234)
    pid = os.getpid()
    answers = iter(
        [
            {
                "status": "ok",
                "server_id": database_identity(db),
                "pid": pid,
            },
            {
                "status": "ok",
                "server_id": database_identity(tmp_path / "replacement.lbdb"),
                "pid": pid,
            },
        ]
    )
    monkeypatch.setattr(admin, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        admin, "probe_health", lambda port, host="127.0.0.1": next(answers)
    )
    monkeypatch.setattr(
        admin.os,
        "kill",
        lambda *args: pytest.fail("ownership changed; must not signal the PID"),
    )

    outcome = admin.stop_server_result(db)

    assert outcome.stopped is False
    assert "ownership changed" in outcome.message
    assert admin.pidfile_path(db).exists()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_install_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)
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


def test_repo_rows_http_uses_registered_host_and_bearer_token(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "columns": ["path", "git_commit", "ingested_at"],
                    "rows": [["/repo", "abc123", "now"]],
                }
            ).encode()

    def open_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(admin._DIRECT_HTTP, "open", open_request)

    rows = admin._repo_rows_http(41234, "::1", "secret-token")

    assert rows == [
        {"path": "/repo", "git_commit": "abc123", "ingested_at": "now"}
    ]
    assert captured == {
        "url": "http://[::1]:41234/api/query",
        "authorization": "Bearer secret-token",
        "timeout": admin._PROBE_TIMEOUT,
    }


# ---------------------------------------------------------------------------
# daemon lifecycle (start / restart / stop --all) + system-wide listing
# ---------------------------------------------------------------------------


def test_start_daemon_reports_already_running(tmp_path, monkeypatch):
    cfg = GragConfig(db_path=tmp_path / "kb.lbdb")
    monkeypatch.setattr(
        admin,
        "find_server",
        lambda db: admin.ServerInfo(port=41999, version="9.9", matches_db=True, pid=123),
    )
    monkeypatch.setattr(
        admin, "_spawn_server_process", lambda *a, **k: pytest.fail("must not spawn")
    )
    msg = admin.start_daemon(cfg)
    assert "Already running" in msg and "41999" in msg


def test_start_daemon_refuses_live_unverified_registration(tmp_path, monkeypatch):
    db = tmp_path / "kb.lbdb"
    admin.write_pidfile(db, 41999)
    monkeypatch.setattr(admin, "find_server", lambda target: None)
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        admin,
        "_spawn_server_process",
        lambda *args, **kwargs: pytest.fail("must not launch a second writer"),
    )

    with pytest.raises(admin.DaemonLifecycleError, match="second writer"):
        admin.start_daemon(GragConfig(db_path=db))


def test_prepare_server_target_removes_only_confirmed_dead_registration(
    tmp_path, monkeypatch
):
    db = tmp_path / "stale.lbdb"
    admin.write_pidfile(db, 41999, shutdown_token="old-token")  # noqa: S106
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)

    admin._prepare_server_target(db)

    assert not admin.pidfile_path(db).exists()


def test_start_daemon_spawns_then_reports_started(tmp_path, monkeypatch):
    cfg = GragConfig(db_path=tmp_path / "kb.lbdb")
    spawned = []
    monkeypatch.setattr(
        admin, "_spawn_server_process", lambda db, port, **k: spawned.append((db, port))
    )
    # not running before spawn, then answers after
    seen = iter([None, admin.ServerInfo(port=42000, version="0.4", matches_db=True, pid=7)])
    monkeypatch.setattr(admin, "find_server", lambda db: next(seen))
    monkeypatch.setattr(admin.time, "sleep", lambda s: None)
    msg = admin.start_daemon(cfg)
    assert spawned and "Started server" in msg and "42000" in msg


def test_list_servers_lists_live_and_reaps_stale(tmp_path, monkeypatch):
    live = tmp_path / "live.lbdb"
    dead = tmp_path / "dead.lbdb"
    admin.write_pidfile(live, 41001)  # written with the current (alive) pid
    admin.write_pidfile(dead, 41002)
    monkeypatch.setattr(
        admin,
        "_pid_alive",
        lambda pid: pid != 41002,
    )
    # dead.lbdb's pidfile stores the current pid too, so force its pid to 41002
    import json as _json

    p = admin.pidfile_path(dead)
    data = _json.loads(p.read_text())
    data["pid"] = 41002
    p.write_text(_json.dumps(data))
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "version": "0.4",
            "server_id": database_identity(live),
            "pid": os.getpid(),
            "mcp_enabled": True,
        }
        if port == 41001
        else None,
    )
    servers = admin.list_servers(current_db=live)
    assert [s.db for s in servers] == [str(live.resolve())]
    assert servers[0].is_current is True
    assert servers[0].verified is True
    assert not admin.pidfile_path(dead).exists()  # stale reaped


def test_stop_all_with_none_running(tmp_path):
    assert "No grag servers running" in admin.stop_all()


def test_windows_liveness_dispatch_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(admin.sys, "platform", "win32")
    monkeypatch.setattr(admin, "_pid_alive_windows", lambda pid: pid == 123)
    monkeypatch.setattr(
        admin.os,
        "kill",
        lambda *args: pytest.fail("Windows liveness must not send a signal"),
    )
    assert admin._pid_alive(123) is True
    assert admin._pid_alive(124) is False


def test_linux_process_state_reads_proc_stat_after_command_name(monkeypatch):
    monkeypatch.setattr(admin.sys, "platform", "linux")
    monkeypatch.setattr(
        admin.Path,
        "read_bytes",
        lambda self: b"123 (name with ) parenthesis) Z 1 2 3",
    )

    assert admin._pid_state_posix(123) == "Z"


def test_macos_process_state_uses_non_signaling_ps(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "Z+  \n"

    monkeypatch.setattr(admin.sys, "platform", "darwin")
    monkeypatch.setattr(
        admin.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Result(),
    )

    assert admin._pid_state_posix(123) == "Z"
    assert calls[0][0] == ["/bin/ps", "-o", "stat=", "-p", "123"]
    assert calls[0][1]["stdin"] is admin.subprocess.DEVNULL


def test_posix_zombie_is_dead_without_signaling(monkeypatch):
    monkeypatch.setattr(admin.sys, "platform", "darwin")
    monkeypatch.setattr(admin, "_pid_state_posix", lambda pid: "Z")
    monkeypatch.setattr(
        admin.os,
        "kill",
        lambda *args: pytest.fail("a zombie must not be signaled"),
    )

    assert admin._pid_alive(123) is False


@pytest.mark.parametrize(("wait_result", "alive"), [(0, False), (0xFFFFFFFF, True)])
def test_windows_wait_failure_is_conservatively_alive(wait_result, alive, monkeypatch):
    import ctypes

    class FakeFunction:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeKernel32:
        OpenProcess = FakeFunction(123)
        WaitForSingleObject = FakeFunction(wait_result)
        CloseHandle = FakeFunction(True)

    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel32(), raising=False
    )
    assert admin._pid_alive_windows(999) is alive


@pytest.mark.parametrize(("last_error", "alive"), [(87, False), (5, True), (8, True)])
def test_windows_open_failure_only_marks_known_missing_pid_dead(
    last_error, alive, monkeypatch
):
    import ctypes

    class FakeFunction:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeKernel32:
        OpenProcess = FakeFunction(0)
        WaitForSingleObject = FakeFunction(0)
        CloseHandle = FakeFunction(True)

    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel32(), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    assert admin._pid_alive_windows(999) is alive


def test_windows_signal_fallback_requires_explicit_force(monkeypatch):
    calls = []
    monkeypatch.setattr(admin.sys, "platform", "win32")
    monkeypatch.setattr(admin.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    sent, detail = admin._signal_process(123, force=False)
    assert sent is False and "--force" in detail and calls == []
    sent, detail = admin._signal_process(123, force=True)
    assert sent is True and detail == "TerminateProcess"
    assert calls == [(123, admin.signal.SIGTERM)]


def test_absurd_pid_is_malformed_not_an_admin_crash(monkeypatch):
    monkeypatch.setattr(admin, "_pid_state_posix", lambda pid: None)
    monkeypatch.setattr(
        admin.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(OverflowError("Python int too large")),
    )
    assert admin._pid_alive(10**100) is False


@pytest.mark.skipif(admin.sys.platform != "win32", reason="Windows liveness smoke test")
def test_windows_real_liveness_probe_is_non_destructive():
    assert admin._pid_alive(os.getpid()) is True


def test_status_tolerates_unrelated_malformed_valid_json(tmp_path, monkeypatch):
    admin.run_dir().mkdir(parents=True)
    (admin.run_dir() / "unrelated.json").write_text(
        json.dumps({"pid": "not-an-integer", "port": 41001}), encoding="utf-8"
    )
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)
    text = "\n".join(admin.status_lines(GragConfig(db_path=tmp_path / "current.lbdb")))
    assert "not running" in text


def test_current_malformed_pidfile_is_reported_and_stop_fails(tmp_path, monkeypatch):
    db = tmp_path / "current.lbdb"
    admin.run_dir().mkdir(parents=True)
    admin.pidfile_path(db).write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)

    text = "\n".join(admin.status_lines(GragConfig(db_path=db)))
    assert "unreadable or malformed" in text
    assert admin.stop_server_result(db).stopped is False
    all_outcome = admin.stop_all_result()
    assert all_outcome.stopped is False
    assert str(admin.pidfile_path(db)) in all_outcome.message


def test_stale_cleanup_failure_is_reported_truthfully(tmp_path, monkeypatch):
    db = tmp_path / "current.lbdb"
    admin.write_pidfile(
        db, 41001, shutdown_token="old-token"  # noqa: S106 — test fixture
    )
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)
    monkeypatch.setattr(
        admin, "_remove_registration_if_matches", lambda *args, **kwargs: False
    )

    outcome = admin.stop_server_result(db)
    text = "\n".join(admin.status_lines(GragConfig(db_path=db)))

    assert outcome.stopped is False
    assert "could not be safely removed" in outcome.message
    assert "could not be safely removed" in text
    assert "removed a stale pidfile" not in text


def test_server_from_dead_registration_exposes_cleanup_failure(
    tmp_path, monkeypatch
):
    db = tmp_path / "current.lbdb"
    admin.write_pidfile(db, 41001)
    path = admin.pidfile_path(db)
    registration = admin.read_pidfile(db)
    assert registration is not None
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        admin, "_remove_registration_if_matches", lambda *args, **kwargs: False
    )

    with pytest.raises(admin._RegistrationCleanupError):
        admin._server_from_registration(path, registration)


def test_status_escapes_malformed_unicode_registration_fields(
    tmp_path, monkeypatch
):
    db = tmp_path / "current.lbdb"
    admin.write_pidfile(db, 41001)
    path = admin.pidfile_path(db)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["host"] = "bad\ud800host"
    data["db"] = "bad\ud800\npath"
    data["started_at"] = "then\udfff\nnow"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(admin, "probe_health", lambda port, host="127.0.0.1": None)

    text = "\n".join(admin.status_lines(GragConfig(db_path=db)))

    text.encode("utf-8")
    assert "\\ud800" in text
    assert "\\udfff" in text
    assert "\\x0a" in text
    assert "\ud800" not in text


def test_registration_with_invalid_database_path_is_unverified_not_a_crash(
    tmp_path, monkeypatch
):
    db = tmp_path / "current.lbdb"
    admin.write_pidfile(db, 41001)
    path = admin.pidfile_path(db)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["db"] = "bad\x00path"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "server_id": "not-the-malformed-path",
            "pid": os.getpid(),
        },
    )

    servers = admin.list_servers(current_db=db)

    assert len(servers) == 1
    assert servers[0].verified is False


def test_stop_all_signals_only_health_verified_registration(tmp_path, monkeypatch):
    verified_db = tmp_path / "verified.lbdb"
    reused_db = tmp_path / "reused.lbdb"
    registrations = [(verified_db, 111, 41001), (reused_db, 222, 41002)]
    for db, pid, port in registrations:
        admin.write_pidfile(db, port)
        path = admin.pidfile_path(db)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["pid"] = pid
        path.write_text(json.dumps(data), encoding="utf-8")

    alive = {111: True, 222: True}
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: alive.get(pid, False))

    def probe(port, host="127.0.0.1"):
        if port == 41001:
            return {
                "status": "ok",
                "server_id": database_identity(verified_db),
                "pid": 111,
                "mcp_enabled": True,
            }
        if port == 41002:
            # The old PID has been reused: a different grag process answers.
            return {
                "status": "ok",
                "server_id": database_identity(reused_db),
                "pid": 999,
                "mcp_enabled": True,
            }
        return None

    monkeypatch.setattr(admin, "probe_health", probe)
    signaled = []

    def kill(pid, sig):
        signaled.append((pid, sig))
        alive[pid] = False

    monkeypatch.setattr(admin.os, "kill", kill)
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: None)
    report = admin.stop_all()

    assert signaled == [(111, admin.signal.SIGTERM)]
    assert "Stopped 1 server" in report
    assert "Refused unverified" in report and "pid 222" in report
    assert not admin.pidfile_path(verified_db).exists()
    assert admin.pidfile_path(reused_db).exists()


def test_spawn_uses_current_python_and_honors_db_dir(tmp_path, monkeypatch):
    db_dir = tmp_path / "dbs"
    calls = []
    monkeypatch.setattr(
        admin.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    admin._spawn_server_process(
        Path("knowledge.lbdb"), 41999, db_dir=db_dir, with_mcp=False
    )
    argv = calls[0][0][0]
    assert argv[:5] == [
        admin.sys.executable,
        "-m",
        "grag.cli",
        "--db-dir",
        str(db_dir.resolve()),
    ]
    assert "--db" not in argv
    assert "--with-mcp" not in argv
    assert calls[0][1]["stdin"] is admin.subprocess.DEVNULL
    assert admin.log_path(db_dir).exists()


def test_spawn_detaches_with_windows_creation_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(admin.sys, "platform", "win32")
    monkeypatch.setattr(
        admin.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    monkeypatch.setattr(admin.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(
        admin.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    admin._spawn_server_process(tmp_path / "kb.lbdb", 41999)

    kwargs = calls[0][1]
    assert kwargs["creationflags"] == 0x00000208
    assert kwargs["stdin"] is admin.subprocess.DEVNULL
    assert "start_new_session" not in kwargs


def test_spawn_starts_child_reaper_for_long_lived_proxy_parent(tmp_path, monkeypatch):
    reaped = []

    class FakeProcess:
        def wait(self):
            reaped.append(True)

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            assert name == "grag-daemon-reaper"
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(admin.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(admin.threading, "Thread", ImmediateThread)

    admin._spawn_server_process(tmp_path / "kb.lbdb", 41999)

    assert reaped == [True]


def test_start_daemon_keys_multi_db_by_directory(tmp_path, monkeypatch):
    db_dir = tmp_path / "dbs"
    cfg = GragConfig(db_dir=db_dir, db_path=Path("knowledge.lbdb"))
    targets = []
    answers = iter(
        [None, admin.ServerInfo(42000, "0.4", True, 77, mcp_enabled=True)]
    )

    def find(target):
        targets.append(target)
        return next(answers)

    spawned = []
    monkeypatch.setattr(admin, "find_server", find)
    monkeypatch.setattr(
        admin,
        "_spawn_server_process",
        lambda db, port, **kwargs: spawned.append((db, port, kwargs)),
    )
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: None)
    message = admin.start_daemon(cfg)

    assert targets == [db_dir, db_dir]
    assert spawned[0][2]["db_dir"] == db_dir
    assert "Started server" in message


def test_restart_aborts_when_verified_old_process_lingers(tmp_path, monkeypatch):
    db = tmp_path / "kb.lbdb"
    admin.write_pidfile(db, 41001)
    path = admin.pidfile_path(db)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = 333
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "server_id": database_identity(db),
            "pid": 333,
            "mcp_enabled": True,
        },
    )
    monkeypatch.setattr(admin.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        admin,
        "_spawn_server_process",
        lambda *args, **kwargs: pytest.fail("restart must not spawn while old PID lives"),
    )

    with pytest.raises(admin.DaemonLifecycleError, match="Restart aborted"):
        admin.restart_daemon(GragConfig(db_path=db))


def test_restart_preserves_recorded_launch_settings_and_accepts_force(
    tmp_path, monkeypatch
):
    db = tmp_path / "kb.lbdb"
    admin.write_pidfile(
        db,
        42017,
        host="::1",
        with_mcp=True,
        mcp_path="/agent-mcp",
        shutdown_token="stop-secret",  # noqa: S106 — test fixture
    )
    stopped = []
    started = []
    monkeypatch.setattr(
        admin,
        "_stop_server",
        lambda target, *, force=False: stopped.append((target, force))
        or admin.StopOutcome("stopped", True),
    )
    monkeypatch.setattr(
        admin,
        "start_daemon",
        lambda config, **kwargs: started.append(kwargs) or "started",
    )

    message = admin.restart_daemon(GragConfig(db_path=db), force=True)

    assert stopped == [(db, True)]
    assert started == [
        {
            "host": "::1",
            "port": 42017,
            "with_mcp": True,
            "mcp_path": "/agent-mcp",
            "wait_seconds": 20.0,
        }
    ]
    assert message == "stopped\nstarted"


def test_status_reports_mcp_off(tmp_path, monkeypatch):
    db = tmp_path / "kb.lbdb"
    monkeypatch.setattr(
        admin,
        "find_server",
        lambda target: admin.ServerInfo(41001, "0.4", True, 123, mcp_enabled=False),
    )
    monkeypatch.setattr(admin, "list_servers", lambda current_db=None: [])
    text = "\n".join(admin.status_lines(GragConfig(db_path=db)))
    assert "mcp:     off" in text
    assert "/mcp" not in text


@pytest.mark.parametrize("flag", ["-a", "-all", "--all"])
def test_stop_all_cli_aliases_are_accepted(flag, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        admin,
        "stop_all_result",
        lambda *, force=False: calls.append(force)
        or admin.StopOutcome("stopped", True),
    )
    assert cli.main(["stop", flag]) == 0
    assert calls == [False]
    assert "stopped" in capsys.readouterr().out


def test_stop_all_force_is_explicit_escape_hatch(tmp_path, monkeypatch):
    db = tmp_path / "legacy.lbdb"
    admin.write_pidfile(db, 41003)
    path = admin.pidfile_path(db)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = 444
    path.write_text(json.dumps(data), encoding="utf-8")

    alive = {444: True}
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: alive.get(pid, False))
    # v0.4.0 health has database_id but does not echo pid/server_id.
    monkeypatch.setattr(
        admin,
        "probe_health",
        lambda port, host="127.0.0.1": {
            "status": "ok",
            "database_id": database_identity(db),
            "version": "0.4.0",
        },
    )

    signaled = []

    def kill(pid, sig):
        signaled.append((pid, sig))
        alive[pid] = False

    monkeypatch.setattr(admin.os, "kill", kill)
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: None)
    report = admin.stop_all(force=True)

    assert signaled == [(444, admin.signal.SIGTERM)]
    assert "FORCED unverified" in report
    assert not path.exists()


def test_stop_all_force_cli_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin,
        "stop_all_result",
        lambda *, force=False: calls.append(force)
        or admin.StopOutcome("stopped", True),
    )
    assert cli.main(["stop", "--all", "--force"]) == 0
    assert calls == [True]


def test_cli_stop_returns_failure_when_any_server_was_not_stopped(monkeypatch, capsys):
    monkeypatch.setattr(
        admin,
        "stop_all_result",
        lambda *, force=False: admin.StopOutcome("refused pid 123", False),
    )
    assert cli.main(["stop", "--all"]) == 1
    assert "refused pid 123" in capsys.readouterr().out


def test_cli_restart_leaves_unspecified_settings_for_preservation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin, "restart_daemon", lambda config, **kwargs: calls.append(kwargs) or "ok"
    )

    assert cli.main(["restart", "--force"]) == 0
    assert calls == [
        {
            "host": None,
            "port": None,
            "with_mcp": None,
            "mcp_path": None,
            "force": True,
        }
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--port=0"],
        ["mcp", "--port=65536"],
        ["start", "--port=0"],
        ["restart", "--port=65536"],
        ["init", "--port=0"],
    ],
)
def test_cli_rejects_out_of_range_ports(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["serve", "start", "restart"])
@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/",
        "/api",
        "/api/admin/stop",
        "/assets",
        "/assets/index.js",
        "//evil.example/mcp",
        "/mcp?target=evil",
        "/mcp#fragment",
        "/mcp\\child",
        "/mcp child",
        "/mcp\x00child",
        "/mcp\ud800",
    ],
)
def test_cli_rejects_mcp_mounts_that_shadow_app_routes(command, path):
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--mcp-path", path])
    assert exc.value.code == 2


@pytest.mark.parametrize("path", ["/mcp", "/agent-mcp", "/assets2"])
def test_cli_accepts_dedicated_mcp_mount_paths(path):
    assert cli._mounted_mcp_path(path) == path


def test_cli_serve_refuses_to_overwrite_existing_registration(monkeypatch, capsys):
    monkeypatch.setattr(admin, "write_pidfile", lambda *args, **kwargs: False)
    assert cli.main(["serve"]) == 1
    assert "Could not safely register" in capsys.readouterr().err


def test_cli_serve_prepares_target_before_atomic_claim(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        admin, "_prepare_server_target", lambda target: calls.append(("prepare", target))
    )
    monkeypatch.setattr(
        admin,
        "write_pidfile",
        lambda target, *args, **kwargs: calls.append(("claim", target)) or False,
    )

    assert cli.main(["serve"]) == 1
    assert [kind for kind, _target in calls] == ["prepare", "claim"]
    assert "Could not safely register" in capsys.readouterr().err


def test_explicit_cli_database_selector_clears_opposite_environment(monkeypatch):
    monkeypatch.setenv("GRAG_DB_PATH", "/env/wrong.lbdb")
    monkeypatch.setenv("GRAG_DB_DIR", "/env/wrong-dbs")

    single = cli._config(
        cli.argparse.Namespace(db="/cli/right.lbdb", db_dir=None)
    )
    assert single.db_path == Path("/cli/right.lbdb")
    assert single.db_dir is None

    multi = cli._config(cli.argparse.Namespace(db=None, db_dir="/cli/right-dbs"))
    assert multi.db_dir == Path("/cli/right-dbs")
    assert multi.db_path == GragConfig().db_path


def test_cli_stop_keys_multi_db_registration_by_directory(tmp_path, monkeypatch):
    db_dir = tmp_path / "dbs"
    targets = []
    monkeypatch.setattr(
        admin,
        "stop_server_result",
        lambda target, *, force=False: targets.append((target, force))
        or admin.StopOutcome("stopped", True),
    )
    assert cli.main(["--db-dir", str(db_dir), "stop"]) == 0
    assert targets == [(db_dir, False)]


def test_real_managed_server_uses_graceful_shutdown_channel(tmp_path):
    """Exercise pidfile token -> HTTP stop -> uvicorn/engine close end to end."""
    db = tmp_path / "managed.lbdb"
    state_dir = admin.GRAG_HOME
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    wrapper = (
        "import sys; from pathlib import Path; import grag.admin as a; "
        "a.GRAG_HOME = Path(sys.argv[1]); from grag.cli import main; "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            wrapper,
            str(state_dir),
            "--db",
            str(db),
            "serve",
            f"--port={port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = admin.find_server(db)
            if info is not None and info.pid == process.pid:
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"server exited early:\n{stdout}\n{stderr}")
            time.sleep(0.1)
        else:
            pytest.fail("managed server did not become ready")

        registration = admin.read_pidfile(db)
        assert registration is not None and registration["shutdown_token"]
        # A long-lived MCP proxy remains the daemon's parent. Its background
        # reaper must make graceful stop observe a real exit without callers
        # manually polling Popen (which is what previously hid zombie PIDs).
        admin._start_process_reaper(process)
        outcome = admin.stop_server_result(db)
        assert outcome.stopped is True
        assert process.wait(timeout=10) == 0
        assert not admin.pidfile_path(db).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
