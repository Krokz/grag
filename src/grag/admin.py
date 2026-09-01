"""Operational helpers: server pidfiles, daemon logs, status / stop / doctor.

Everything here is about *managing* a local grag server, not serving data:

* ``grag serve`` registers itself in a pidfile under ``~/.grag/run/`` so
  ``grag status`` / ``grag stop`` can find it later.
* Auto-served daemons (grag.proxy) log to ``~/.grag/logs/`` instead of
  /dev/null, so "vector": "error" and startup failures are debuggable.
* ``grag doctor`` reports install health: extras, embedder, db file, server
  reachability, and code-index staleness per ingested repo.

HTTP probing uses the server's recorded bind address with environment proxies
disabled, and relies on stdlib urllib so the default install needs no extra
dependency.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from grag.config import GragConfig, database_identity, derive_port

GRAG_HOME = Path.home() / ".grag"

_PROBE_TIMEOUT = 2.0
_DIRECT_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def run_dir() -> Path:
    return GRAG_HOME / "run"


def log_dir() -> Path:
    return GRAG_HOME / "logs"


def _identity8(db_path: Path) -> str:
    return database_identity(db_path)[:8]


def server_target(config: GragConfig) -> Path:
    """Path whose identity represents one server process.

    Single-database servers are keyed by their ``.lbdb`` path; multi-database
    servers are keyed by the directory they serve.
    """
    return config.db_dir if config.db_dir is not None else config.db_path


def pidfile_path(db_path: Path) -> Path:
    return run_dir() / f"{_identity8(db_path)}.json"


def log_path(db_path: Path) -> Path:
    stem = db_path.stem or "grag"
    return log_dir() / f"{stem}-{_identity8(db_path)}.log"


def open_daemon_log(db_path: Path) -> int:
    """File descriptor for a daemon's stdout/stderr; DEVNULL on any failure."""
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        path = log_path(db_path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return subprocess.DEVNULL
    return fd


# ---------------------------------------------------------------------------
# pidfile
# ---------------------------------------------------------------------------


def _registration_lock_path(path: Path) -> Path:
    """Persistent advisory-lock path for one registration target."""
    return path.with_suffix(".lock")


@contextlib.contextmanager
def _registration_lock(path: Path) -> Iterator[None]:
    """Serialize registration reads, claims, and conditional removals.

    The lock file intentionally persists: deleting a lock file while another
    process holds it can create two independently locked inodes for one target.
    OS advisory locks are released automatically if a process crashes.
    """
    lock_path = _registration_lock_path(path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if sys.platform == "win32":
            import msvcrt

            # msvcrt locks a byte range from the current file position.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            if sys.platform == "win32":
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_pidfile(
    db_path: Path,
    port: int,
    *,
    is_db_dir: bool = False,
    with_mcp: bool = False,
    mcp_path: str | None = None,
    host: str = "127.0.0.1",
    shutdown_token: str | None = None,
) -> bool:
    """Atomically claim and record one server target.

    A live existing registration is never overwritten: doing so would lose its
    shutdown token and let a failed second server erase the first server's
    registration on exit.
    """
    try:
        run_dir().mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "db": str(db_path.resolve()) if str(db_path) != ":memory:" else ":memory:",
                "is_db_dir": is_db_dir,
                "with_mcp": with_mcp,
                "mcp_path": mcp_path if with_mcp else None,
                "host": host,
                "shutdown_token": shutdown_token,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
    except OSError:
        return False

    path = pidfile_path(db_path)
    try:
        with _registration_lock(path):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError:
                return False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    with contextlib.suppress(OSError):
                        os.fchmod(handle.fileno(), 0o600)
                    handle.write(payload + "\n")
            except OSError:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return False
    except OSError:
        return False
    return True


def remove_pidfile(
    db_path: Path,
    *,
    owner_pid: int | None = None,
    shutdown_token: str | None = None,
) -> None:
    """Remove a registration, optionally only when it is still ours."""
    path = pidfile_path(db_path)
    if owner_pid is not None:
        _remove_registration_if_matches(
            path, owner_pid=owner_pid, shutdown_token=shutdown_token
        )
    else:
        try:
            with _registration_lock(path):
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_registration_if_matches(
    path: Path, *, owner_pid: int, shutdown_token: object
) -> bool:
    """Ensure the previously-read owner's registration is safely absent."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _registration_lock(path):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return True
            except (OSError, ValueError):
                return False
            current_pid = current.get("pid") if isinstance(current, dict) else None
            if (
                not isinstance(current_pid, int)
                or isinstance(current_pid, bool)
                or current_pid != owner_pid
                or current.get("shutdown_token") != shutdown_token
            ):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                return True
            except OSError:
                return False
    except OSError:
        return False
    return True


def _read_registration(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with _registration_lock(path):
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "pid" in data else None


def read_pidfile(db_path: Path) -> dict | None:
    return _read_registration(pidfile_path(db_path))


def _pid_alive_windows(pid: int) -> bool:
    """Check a Windows PID without sending a signal.

    Unlike POSIX, ``os.kill(pid, 0)`` calls ``TerminateProcess`` on Windows.
    A zero-timeout wait on a process handle is a read-only liveness probe.
    """
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is how OpenProcess reports a nonexistent PID.
        # Access denied and other failures are not proof of exit, so keep the
        # registration and require health/PID verification before stopping it.
        return ctypes.get_last_error() != 87  # type: ignore[attr-defined]
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        # WAIT_FAILED (and any undocumented result) is not evidence that the
        # process exited. Treat it as alive so a transient probe failure can
        # never reap a live registration or enable PID reuse hazards.
        return result != wait_object_0
    finally:
        kernel32.CloseHandle(handle)


def _pid_state_posix(pid: int) -> str | None:
    """Return a POSIX process state without signaling the process.

    Linux exposes the state in procfs. macOS and other POSIX platforms use
    ``ps`` because they do not expose ``/proc/<pid>/stat``. Inspection failure
    is deliberately inconclusive; callers retain the conservative liveness
    fallback.
    """
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            return None
        _prefix, separator, fields = stat.rpartition(b") ")
        return chr(fields[0]) if separator and fields else None

    try:
        result = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    state = result.stdout.strip()
    return state[0] if result.returncode == 0 and state else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return _pid_alive_windows(pid)
        except (OverflowError, ValueError):
            return False
    # Zombies still have a PID and make kill(pid, 0) succeed, but cannot serve
    # requests or handle shutdown. Treat only an explicitly observed Z state as
    # dead; all inspection failures fall through to the conservative probe.
    if _pid_state_posix(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except (OverflowError, ValueError):
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


def _connect_host(bind_host: str) -> str:
    """Address used to reach a server bound on ``bind_host`` locally."""
    host = bind_host.strip()
    if host in {"", "0.0.0.0"}:  # noqa: S104 — translating a bind wildcard
        return "127.0.0.1"
    if host == "::":
        return "::1"
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _http_origin(host: str, port: int) -> str | None:
    """Build a local server origin, including brackets for IPv6 literals."""
    host = _connect_host(host)
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not host
        or any(char in host for char in "/@?#")
        or any(
            char.isspace()
            or ord(char) < 0x20
            or ord(char) == 0x7F
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in host
        )
    ):
        return None
    url_host = f"[{host}]" if ":" in host else host
    return f"http://{url_host}:{port}"


def _safe_display_text(value: object, default: str = "?") -> str:
    """Render untrusted registration/health text without terminal injection.

    JSON can legally decode lone UTF-16 surrogate escapes, but writing such a
    Python string to a UTF-8 terminal raises ``UnicodeEncodeError``. Control
    characters are escaped at the same boundary so a malformed registration
    cannot forge additional status lines.
    """
    if not isinstance(value, str):
        return default
    rendered: list[str] = []
    for char in value:
        codepoint = ord(char)
        if codepoint < 0x20 or codepoint == 0x7F:
            rendered.append(f"\\x{codepoint:02x}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def probe_health(port: int, host: str = "127.0.0.1") -> dict | None:
    """GET /api/health from a registered bind address; None if unreachable."""
    origin = _http_origin(host, port)
    if origin is None:
        return None
    url = f"{origin}/api/health"
    try:
        with _DIRECT_HTTP.open(url, timeout=_PROBE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


@dataclass
class ServerInfo:
    port: int
    version: str | None
    matches_db: bool
    pid: int | None  # from pidfile, when one exists and the pid is alive
    mcp_enabled: bool | None = None
    mcp_path: str | None = None
    host: str = "127.0.0.1"


def _int_field(value: object) -> int | None:
    """Return a strict JSON integer (booleans are not process IDs/ports)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _health_identity(health: dict) -> str | None:
    identity = health.get("server_id")
    if not isinstance(identity, str):
        identity = health.get("database_id")
    return identity if isinstance(identity, str) else None


def find_server(db_path: Path) -> ServerInfo | None:
    """Locate a running server for a database or multi-database directory.

    Tries, in order: the port recorded in the pidfile, the derived
    per-database port, and the legacy default 8471. A server "matches" when
    its /api/health server identity equals db_path's identity. ``database_id``
    remains a compatibility fallback for production v0.4.0 servers.
    """
    expected = database_identity(db_path)
    pidinfo = read_pidfile(db_path)
    recorded_host_value = pidinfo.get("host") if pidinfo else None
    recorded_host = (
        recorded_host_value
        if isinstance(recorded_host_value, str)
        else "127.0.0.1"
    )
    candidates: list[tuple[str, int]] = []
    recorded_port = _int_field(pidinfo.get("port")) if pidinfo else None
    if recorded_port is not None and 1 <= recorded_port <= 65535:
        candidates.append((recorded_host, recorded_port))
    for port in (derive_port(db_path), 8471):
        candidate = ("127.0.0.1", port)
        if candidate not in candidates:
            candidates.append(candidate)
    for host, port in candidates:
        health = probe_health(port, host)
        if health is None:
            continue
        if _health_identity(health) == expected:
            pid = None
            recorded_pid = _int_field(pidinfo.get("pid")) if pidinfo else None
            health_pid = _int_field(health.get("pid"))
            if (
                recorded_pid is not None
                and recorded_pid == health_pid
                and _pid_alive(recorded_pid)
            ):
                pid = recorded_pid
            version = health.get("version")
            mcp_enabled = health.get("mcp_enabled")
            mcp_path = health.get("mcp_path")
            return ServerInfo(
                port=port,
                version=version if isinstance(version, str) else None,
                matches_db=True,
                pid=pid,
                mcp_enabled=mcp_enabled if isinstance(mcp_enabled, bool) else None,
                mcp_path=mcp_path if isinstance(mcp_path, str) else None,
                host=host,
            )
    return None


@dataclass
class RunningServer:
    pid: int
    port: int | None
    host: str
    db: str | None
    is_db_dir: bool
    started_at: str | None
    healthy: bool  # /api/health answered on the recorded port
    verified: bool  # health PID and server identity match this registration
    version: str | None
    mcp_enabled: bool | None
    mcp_path: str | None
    shutdown_token: str | None
    is_current: bool  # matches the db_path status was invoked for
    registration_path: Path


class _RegistrationCleanupError(RuntimeError):
    """A dead registration could not be removed without risking its replacement."""


def _health_verifies_registration(
    health: dict | None, *, pid: int, db: str | None
) -> bool:
    if health is None or db is None:
        return False
    health_pid = _int_field(health.get("pid"))
    try:
        expected_identity = database_identity(Path(db))
    except (OSError, ValueError):
        return False
    return health_pid == pid and _health_identity(health) == expected_identity


def _server_from_registration(
    path: Path, data: dict, *, current_id: str | None = None
) -> RunningServer | None:
    pid = _int_field(data.get("pid"))
    if pid is None or pid <= 0:
        return None
    if not _pid_alive(pid):
        removed = _remove_registration_if_matches(
            path, owner_pid=pid, shutdown_token=data.get("shutdown_token")
        )
        if not removed:
            raise _RegistrationCleanupError(
                f"Dead registration {path} changed or could not be safely removed."
            )
        return None
    port = _int_field(data.get("port"))
    if port is not None and not 1 <= port <= 65535:
        port = None
    host_value = data.get("host")
    host = host_value if isinstance(host_value, str) else "127.0.0.1"
    health = probe_health(port, host) if port is not None else None
    db = data.get("db") if isinstance(data.get("db"), str) else None
    version = health.get("version") if health else None
    mcp_enabled = health.get("mcp_enabled") if health else None
    mcp_path = health.get("mcp_path") if health else None
    return RunningServer(
        pid=pid,
        port=port,
        host=host,
        db=db,
        is_db_dir=data.get("is_db_dir") is True,
        started_at=data.get("started_at")
        if isinstance(data.get("started_at"), str)
        else None,
        healthy=health is not None,
        verified=_health_verifies_registration(health, pid=pid, db=db),
        version=version if isinstance(version, str) else None,
        mcp_enabled=mcp_enabled if isinstance(mcp_enabled, bool) else None,
        mcp_path=mcp_path if isinstance(mcp_path, str) else None,
        shutdown_token=data.get("shutdown_token")
        if isinstance(data.get("shutdown_token"), str)
        else None,
        is_current=current_id is not None and path.stem == current_id,
        registration_path=path,
    )


def list_servers(current_db: Path | None = None) -> list[RunningServer]:
    """Every live grag server registered under ~/.grag/run/, system-wide.

    One pidfile per server target is written by ``grag serve``; entries whose
    pid is no longer alive are treated as stale and their pidfile is removed.
    A live PID is shown even when health cannot verify that it is still the
    registered grag process, but stop operations will refuse to signal it.
    """
    current_id = _identity8(current_db) if current_db is not None else None
    servers: list[RunningServer] = []
    for path in sorted(run_dir().glob("*.json")):
        data = _read_registration(path)
        if data is None:
            continue
        try:
            server = _server_from_registration(path, data, current_id=current_id)
        except _RegistrationCleanupError:
            continue
        if server is not None:
            servers.append(server)
    servers.sort(key=lambda s: (s.port is None, s.port or 0))
    return servers


# ---------------------------------------------------------------------------
# status / stop
# ---------------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def status_lines(config: GragConfig) -> list[str]:
    """Human-readable status for the configured database."""
    target = server_target(config)
    target_label = "database directory" if config.db_dir is not None else "database"
    lines = [f"{target_label}:  {target}"]
    if str(target) != ":memory:":
        resolved = target.resolve()
        if resolved.exists():
            if resolved.is_dir():
                lines.append("  exists:  yes")
            else:
                lines.append(f"  exists:  yes ({_fmt_size(resolved.stat().st_size)})")
        else:
            lines.append("  exists:  no (created on first write)")
    lines.append(f"  id:      {_identity8(target)}")

    info = find_server(target)
    if info is not None:
        pid = f", pid {info.pid}" if info.pid else ""
        origin = _http_origin(info.host, info.port) or f"port {info.port}"
        version = _safe_display_text(info.version or "?")
        lines.append(
            f"server:    running on {origin}/ "
            f"(grag {version}{pid})"
        )
        lines.append(f"  ui:      {origin}/")
        if info.mcp_enabled is True:
            lines.append(
                f"  mcp:     {origin}{_safe_display_text(info.mcp_path or '/mcp')}"
            )
        elif info.mcp_enabled is False:
            lines.append("  mcp:     off")
        else:
            lines.append("  mcp:     unknown (server does not report capability)")
    else:
        lines.append("server:    not running")
        stale = read_pidfile(target)
        registration_exists = pidfile_path(target).exists()
        stale_pid = _int_field(stale.get("pid")) if stale else None
        if stale is None and registration_exists:
            lines.append("  (pidfile is unreadable or malformed; ignored)")
        elif stale is not None and stale_pid is None:
            lines.append("  (pidfile is malformed; ignored)")
        elif stale_pid is not None and not _pid_alive(stale_pid):
            removed = _remove_registration_if_matches(
                pidfile_path(target),
                owner_pid=stale_pid,
                shutdown_token=stale.get("shutdown_token") if stale else None,
            )
            if removed:
                lines.append("  (removed a stale pidfile from a previous run)")
            else:
                lines.append(
                    "  (stale pidfile changed or could not be safely removed)"
                )
    log = log_path(target)
    if log.exists():
        lines.append(f"log:       {log}")
    if config.embedder is not None:
        lines.append(
            f"embedder:  {config.embedder.provider} ({config.embedder.model})"
        )
    else:
        lines.append("embedder:  off — FTS-only retrieval (set GRAG_EMBED_PROVIDER)")

    servers = list_servers(current_db=target)
    if servers:
        lines.append(f"all grag servers on this system ({len(servers)}):")
        for s in servers:
            marker = " *" if s.is_current else "  "
            where = (
                f"{_http_origin(s.host, s.port)}/"
                if s.port and _http_origin(s.host, s.port)
                else "port ?"
            )
            ver = (
                f"grag {_safe_display_text(s.version)}"
                if s.version
                else ("grag ?" if s.healthy else "no health response")
            )
            if not s.verified:
                ver += ", unverified registration"
            db_txt = _safe_display_text(s.db)
            started = (
                f", since {_safe_display_text(s.started_at)}" if s.started_at else ""
            )
            lines.append(f"{marker} pid {s.pid}  {where}  ({ver}{started})")
            kind = "db-dir" if s.is_db_dir else "db"
            lines.append(f"     {kind}: {db_txt}")
        if any(s.is_current for s in servers):
            lines.append("  (* = this database)")
    return lines


@dataclass(frozen=True)
class StopOutcome:
    message: str
    stopped: bool


def _request_graceful_shutdown(server: RunningServer) -> bool:
    """Ask a managed server to close uvicorn and its engine cleanly."""
    if server.port is None or not server.shutdown_token:
        return False
    origin = _http_origin(server.host, server.port)
    if origin is None:
        return False
    try:
        request = urllib.request.Request(  # noqa: S310 — validated HTTP origin
            f"{origin}/api/admin/stop",
            data=b"",
            headers={"x-grag-stop-token": server.shutdown_token},
            method="POST",
        )
        with _DIRECT_HTTP.open(request, timeout=_PROBE_TIMEOUT) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_for_exit(pid: int) -> bool:
    for _ in range(20):
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return not _pid_alive(pid)


def _signal_process(pid: int, *, force: bool) -> tuple[bool, str]:
    """Fallback for legacy servers without the graceful control endpoint."""
    if sys.platform == "win32" and not force:
        return (
            False,
            (
                "the server has no working graceful shutdown channel; refusing a "
                "Windows TerminateProcess fallback without --force"
            ),
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "already gone"
    except (PermissionError, OSError) as exc:
        return False, str(exc)
    action = "TerminateProcess" if sys.platform == "win32" else "SIGTERM"
    return True, action


def _stop_registration(server: RunningServer, *, force: bool) -> StopOutcome:
    """Stop a parsed registration, preferring an authenticated clean shutdown."""
    if not server.verified and not force:
        return StopOutcome(
            f"Refusing to signal pid {server.pid}: health did not verify that it is "
            "the registered grag server for this target.",
            False,
        )

    graceful = server.verified and _request_graceful_shutdown(server)
    action = "graceful shutdown"
    if not graceful:
        if server.verified:
            health = (
                probe_health(server.port, server.host)
                if server.port is not None
                else None
            )
            if not _health_verifies_registration(
                health, pid=server.pid, db=server.db
            ):
                return StopOutcome(
                    f"Refusing the signal fallback for pid {server.pid}: server "
                    "ownership changed after the initial health verification.",
                    False,
                )
        signaled, action = _signal_process(server.pid, force=force)
        if not signaled:
            return StopOutcome(f"Could not stop pid {server.pid}: {action}.", False)

    if not _wait_for_exit(server.pid):
        return StopOutcome(
            f"{'FORCED: ' if not server.verified else ''}Requested {action} for pid "
            f"{server.pid}; it has not exited yet.",
            False,
        )
    removed = _remove_registration_if_matches(
        server.registration_path,
        owner_pid=server.pid,
        shutdown_token=server.shutdown_token,
    )
    prefix = "FORCED unverified stop: " if not server.verified else ""
    if not removed:
        return StopOutcome(
            f"{prefix}Server pid {server.pid} exited, but its registration changed "
            "or could not be safely removed.",
            False,
        )
    return StopOutcome(f"{prefix}Stopped server (pid {server.pid}).", True)


def _stop_server(db_path: Path, *, force: bool = False) -> StopOutcome:
    """Stop one verified registration and expose success to restart."""
    info = read_pidfile(db_path)
    if info is None:
        if find_server(db_path) is not None:
            return StopOutcome(
                "A matching server is running without a verifiable pidfile; "
                "refusing to signal an unknown process.",
                False,
            )
        if pidfile_path(db_path).exists():
            return StopOutcome(
                "Pidfile is unreadable or malformed; refusing to treat the stop "
                "as successful.",
                False,
            )
        return StopOutcome(
            "No pidfile for this server target — nothing to stop.", True
        )
    pid = _int_field(info.get("pid"))
    if pid is None or pid <= 0:
        return StopOutcome(
            "Pidfile is malformed; refusing to signal an unknown process.", False
        )
    if not _pid_alive(pid):
        removed = _remove_registration_if_matches(
            pidfile_path(db_path),
            owner_pid=pid,
            shutdown_token=info.get("shutdown_token"),
        )
        if not removed:
            return StopOutcome(
                "Server is already gone, but its registration changed or could "
                "not be safely removed.",
                False,
            )
        return StopOutcome("Server already gone (removed stale pidfile).", True)

    try:
        registered = _server_from_registration(pidfile_path(db_path), info)
    except _RegistrationCleanupError as exc:
        return StopOutcome(str(exc), False)
    if registered is None:
        return StopOutcome("Server already gone (removed stale pidfile).", True)
    return _stop_registration(registered, force=force)


def stop_server(db_path: Path, *, force: bool = False) -> str:
    """Stop a health/PID-verified server for one target."""
    return _stop_server(db_path, force=force).message


def stop_server_result(db_path: Path, *, force: bool = False) -> StopOutcome:
    """Structured variant used by the CLI to return a truthful exit status."""
    return _stop_server(db_path, force=force)


def _stop_all(*, force: bool = False) -> StopOutcome:
    """Stop every verified registered grag server; return a structured report.

    Live PIDs without a health response that echoes both the PID and target
    identity are reported and skipped. This prevents stale pidfiles plus PID
    reuse from terminating unrelated processes.
    """
    servers = list_servers()
    registered_paths = set(run_dir().glob("*.json"))
    unresolved = sorted(
        registered_paths - {server.registration_path for server in servers}
    )
    if not servers and not unresolved:
        return StopOutcome("No grag servers running.", True)
    stopped: list[RunningServer] = []
    failed: list[tuple[RunningServer, str]] = []
    unverified = [server for server in servers if not server.verified]
    forced: list[RunningServer] = []
    for s in servers:
        if not s.verified and not force:
            continue
        if not s.verified:
            forced.append(s)
        outcome = _stop_registration(s, force=force)
        if outcome.stopped:
            stopped.append(s)
        else:
            failed.append((s, outcome.message))
    report = (
        [f"Stopped {len(stopped)} server(s): " + ", ".join(f"pid {x.pid}" for x in stopped)]
        if stopped
        else []
    )
    if failed:
        report.extend(message for _, message in failed)
    if forced:
        report.append(
            "FORCED unverified registration(s): "
            + ", ".join(f"pid {x.pid}" for x in forced)
        )
    elif unverified:
        report.append(
            "Refused unverified registration(s): "
            + ", ".join(f"pid {x.pid}" for x in unverified)
            + ". Health must echo the registered PID and server target identity."
        )
    if unresolved:
        report.append(
            "Refused unreadable, malformed, or concurrently changed registration(s): "
            + ", ".join(str(path) for path in unresolved)
            + ". Inspect and remove only the exact stale files after confirming no "
            "matching process is running."
        )
    message = "\n".join(report) or "No verified grag servers could be stopped."
    succeeded = not failed and not unresolved and (force or not unverified)
    return StopOutcome(message, succeeded)


def stop_all_result(*, force: bool = False) -> StopOutcome:
    """Structured variant used by the CLI to return a truthful exit status."""
    return _stop_all(force=force)


def stop_all(*, force: bool = False) -> str:
    """Stop every verified registration and return a human-readable report."""
    return _stop_all(force=force).message


# ---------------------------------------------------------------------------
# daemon lifecycle (grag start / restart)
# ---------------------------------------------------------------------------


class DaemonLifecycleError(RuntimeError):
    """A requested daemon transition could not be completed safely."""


def _prepare_server_target(target: Path) -> None:
    """Remove one confirmed-dead registration or refuse an ambiguous owner."""
    path = pidfile_path(target)
    if not path.exists():
        return
    registration = read_pidfile(target)
    if registration is None:
        raise DaemonLifecycleError(
            "An unreadable or malformed pidfile already exists for this server "
            "target. Inspect 'grag status' and remove that exact stale file before "
            "starting another writer."
        )
    registered_pid = _int_field(registration.get("pid"))
    if registered_pid is None or registered_pid <= 0:
        raise DaemonLifecycleError(
            "A malformed pidfile already exists for this server target; refusing "
            "to overwrite an ambiguous registration."
        )
    if _pid_alive(registered_pid):
        raise DaemonLifecycleError(
            f"Pid {registered_pid} is still registered for this server target. "
            "Refusing to launch a second writer. Inspect 'grag status', then use "
            "'grag stop --force' only after confirming that PID if health cannot "
            "verify it."
        )
    removed = _remove_registration_if_matches(
        path,
        owner_pid=registered_pid,
        shutdown_token=registration.get("shutdown_token"),
    )
    if not removed:
        raise DaemonLifecycleError(
            "The server registration changed while stale-state cleanup was in "
            "progress; refusing to launch another writer. Retry after 'grag status'."
        )


def _start_process_reaper(process: object) -> None:
    """Reap a spawned daemon if this parent stays alive (notably MCP proxy).

    ``start_new_session`` detaches terminal/session state but does not reparent
    the child. A long-lived proxy therefore remains its parent; without a wait,
    a stopped server can stay as a zombie and make PID liveness checks report a
    false lingering process. The thread is daemonized so a short-lived `start`
    command can still exit and let the OS adopt the server normally.
    """
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return

    def reap() -> None:
        with contextlib.suppress(Exception):
            wait()

    threading.Thread(
        target=reap,
        name="grag-daemon-reaper",
        daemon=True,
    ).start()


def _spawn_server_process(
    db_path: Path,
    port: int,
    *,
    db_dir: Path | None = None,
    with_mcp: bool = True,
    mcp_path: str = "/mcp",
    host: str = "127.0.0.1",
) -> None:
    """Launch a detached ``grag serve`` daemon. Output goes to ~/.grag/logs/
    (not /dev/null) so embedder failures and startup errors stay debuggable.
    Env is inherited, so ``GRAG_EMBED_PROVIDER=fastembed grag start`` carries
    the embedder into the daemon. The current interpreter/module is used so a
    development checkout cannot accidentally launch a different ``grag`` from
    PATH. Shared by the CLI and the MCP proxy."""
    target = db_dir if db_dir is not None else db_path
    _prepare_server_target(target)
    target_arg = (
        str(target.resolve()) if str(target) != ":memory:" else ":memory:"
    )
    selector = "--db-dir" if db_dir is not None else "--db"
    argv = [
        sys.executable,
        "-m",
        "grag.cli",
        selector,
        target_arg,
        "serve",
        f"--host={host}",
        f"--port={port}",
    ]
    if with_mcp:
        argv.extend(["--with-mcp", f"--mcp-path={mcp_path}"])
    log_fd = open_daemon_log(target)
    try:
        if sys.platform == "win32":
            # The authenticated HTTP control endpoint provides graceful
            # shutdown, so the daemon can be fully detached from its console.
            creationflags = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            ) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                creationflags=creationflags,
            )
        else:
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
            )
        _start_process_reaper(process)
    finally:
        if isinstance(log_fd, int) and log_fd >= 0:
            os.close(log_fd)


def start_daemon(
    config: GragConfig,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    with_mcp: bool = True,
    mcp_path: str = "/mcp",
    wait_seconds: float = 20.0,
) -> str:
    """Start a background server for the configured database target.

    Port defaults to the per-target derived port so each database (or database
    directory) gets a stable port that ``status`` / ``stop`` can rediscover.
    """
    target = server_target(config)
    existing = find_server(target)
    if existing is not None:
        origin = _http_origin(existing.host, existing.port) or f"port {existing.port}"
        return (
            f"Already running on {origin}/ "
            f"(grag {existing.version or '?'}"
            f"{f', pid {existing.pid}' if existing.pid else ''}). "
            "Use 'grag restart' to relaunch."
        )
    _prepare_server_target(target)
    if port is None:
        port = derive_port(target)
    _spawn_server_process(
        config.db_path,
        port,
        db_dir=config.db_dir,
        with_mcp=with_mcp,
        mcp_path=mcp_path,
        host=host,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        info = find_server(target)
        if info is not None and info.pid is not None:
            origin = _http_origin(info.host, info.port) or f"port {info.port}"
            mcp = f" · mcp {origin}{mcp_path}" if with_mcp else ""
            return (
                f"Started server (grag {info.version or '?'}"
                f"{f', pid {info.pid}' if info.pid else ''}) on "
                f"{origin}/{mcp}\n"
                f"  log: {log_path(target)}"
            )
    raise DaemonLifecycleError(
        f"Launched daemon but it did not answer on port {port} within "
        f"{wait_seconds:.0f}s with a verified PID. Check the log: {log_path(target)}"
    )


def restart_daemon(
    config: GragConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    with_mcp: bool | None = None,
    mcp_path: str | None = None,
    force: bool = False,
    wait_seconds: float = 20.0,
) -> str:
    """Restart while preserving launch settings that were not overridden."""
    target = server_target(config)
    registration = read_pidfile(target)
    if registration is not None:
        recorded_port = _int_field(registration.get("port"))
        if port is None and recorded_port is not None and 1 <= recorded_port <= 65535:
            port = recorded_port
        recorded_host = registration.get("host")
        if host is None and isinstance(recorded_host, str):
            host = recorded_host
        recorded_mcp = registration.get("with_mcp")
        if with_mcp is None and isinstance(recorded_mcp, bool):
            with_mcp = recorded_mcp
        recorded_mcp_path = registration.get("mcp_path")
        if mcp_path is None and isinstance(recorded_mcp_path, str):
            mcp_path = recorded_mcp_path
    outcome = _stop_server(target, force=force)
    if not outcome.stopped:
        raise DaemonLifecycleError(f"{outcome.message}\nRestart aborted.")
    start_msg = start_daemon(
        config,
        host="127.0.0.1" if host is None else host,
        port=port,
        with_mcp=True if with_mcp is None else with_mcp,
        mcp_path="/mcp" if mcp_path is None else mcp_path,
        wait_seconds=wait_seconds,
    )
    return f"{outcome.message}\n{start_msg}"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _check(label: str, ok: bool, detail_ok: str, detail_bad: str) -> str:
    mark = "ok" if ok else "--"
    return f"  [{mark}] {label}: {detail_ok if ok else detail_bad}"


def _git_commits_behind(repo_path: str, commit: str) -> int | None:
    """How many commits repo_path has on HEAD since `commit`; None if unknown."""
    try:
        out = subprocess.run(  # noqa: S603 — fixed git argv, repo path from the db
            ["git", "-C", repo_path, "rev-list", "--count", f"{commit}..HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def _repo_staleness_lines(rows: list[dict]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        path = row.get("path")
        commit = row.get("git_commit")
        ingested = row.get("ingested_at") or "unknown time"
        if not path:
            continue
        if not commit:
            lines.append(
                f"  [--] {path}: no git commit recorded (re-ingest to enable "
                "staleness tracking)"
            )
            continue
        behind = _git_commits_behind(str(path), str(commit))
        if behind is None:
            lines.append(f"  [??] {path}: ingested {ingested}; git state unknown")
        elif behind == 0:
            lines.append(f"  [ok] {path}: index at HEAD (ingested {ingested})")
        else:
            lines.append(
                f"  [--] {path}: index is {behind} commit(s) behind HEAD "
                f"(ingested {ingested}) — re-run ingest-code"
            )
    return lines


def _repo_rows_http(
    port: int,
    host: str = "127.0.0.1",
    api_token: str | None = None,
) -> list[dict] | None:
    """Repo staleness props via a running server; None if the query failed."""
    origin = _http_origin(host, port)
    if origin is None:
        return None
    body = json.dumps(
        {
            "cypher": (
                "MATCH (r:Repo) RETURN r.path AS path, "
                "r.git_commit AS git_commit, r.ingested_at AS ingested_at"
            )
        }
    ).encode("utf-8")
    headers = {"content-type": "application/json"}
    if api_token:
        headers["authorization"] = f"Bearer {api_token}"
    try:
        req = urllib.request.Request(  # noqa: S310 — validated registered HTTP origin
            f"{origin}/api/query", data=body, headers=headers
        )
        with _DIRECT_HTTP.open(req, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        cols = payload["columns"]
        return [dict(zip(cols, row, strict=True)) for row in payload["rows"]]
    except (KeyError, TypeError, ValueError):
        return None


def doctor_lines(config: GragConfig) -> list[str]:
    """Full install/runtime health report."""
    import grag

    lines = [f"grag {grag.__version__} — python {sys.version.split()[0]} on {sys.platform}"]
    lines.append("")
    lines.append("install:")
    lines.append(
        _check(
            "core engine (ladybug)",
            _module_available("ladybug"),
            "installed",
            "MISSING — reinstall gragdb",
        )
    )
    lines.append(
        _check(
            "local embeddings (fastembed)",
            _module_available("fastembed"),
            "installed — semantic search available",
            "not installed — pip install 'gragdb[embed-local]' for semantic search",
        )
    )
    lines.append(
        _check(
            "tree-sitter code parsing",
            _module_available("tree_sitter"),
            "installed — ts/js/cs/tf/go parsing available",
            "not installed — pip install 'gragdb[code]' for non-Python repos",
        )
    )
    env = {k: v for k, v in os.environ.items() if k.startswith("GRAG_")}
    if env:
        lines.append("")
        lines.append("environment:")
        for k in sorted(env):
            shown = "<set>" if "TOKEN" in k or "KEY" in k else env[k]
            lines.append(f"  {k}={shown}")
    lines.append("")
    lines.extend(status_lines(config))

    # Code-index staleness: prefer the running server (no lock contention);
    # fall back to opening the db directly only when no server holds it.
    repo_rows: list[dict] | None = None
    info = find_server(server_target(config))
    if info is not None:
        repo_rows = _repo_rows_http(info.port, info.host, config.api_token)
    elif (
        config.db_dir is None
        and str(config.db_path) != ":memory:"
        and config.db_path.resolve().exists()
    ):
        repo_rows = _repo_rows_engine(config)
    if repo_rows:
        lines.append("")
        lines.append("code index:")
        lines.extend(_repo_staleness_lines(repo_rows))
    return lines


def _repo_rows_engine(config: GragConfig) -> list[dict] | None:
    from grag.core.engine import Engine
    from grag.core.errors import GragError

    try:
        engine = Engine(config)
    except Exception:  # noqa: BLE001 — locked/corrupt db is a report, not a crash
        return None
    try:
        res = engine.execute(
            "MATCH (r:Repo) RETURN r.path AS path, "
            "r.git_commit AS git_commit, r.ingested_at AS ingested_at"
        )
        return res.as_dicts()
    except GragError:
        return None  # no Repo table (nothing ingested) — fine
    finally:
        engine.close()
