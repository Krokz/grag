"""Operational helpers: server pidfiles, daemon logs, status / stop / doctor.

Everything here is about *managing* a local grag server, not serving data:

* ``grag serve`` registers itself in a pidfile under ``~/.grag/run/`` so
  ``grag status`` / ``grag stop`` can find it later.
* Auto-served daemons (grag.proxy) log to ``~/.grag/logs/`` instead of
  /dev/null, so "vector": "error" and startup failures are debuggable.
* ``grag doctor`` reports install health: extras, embedder, db file, server
  reachability, and code-index staleness per ingested repo.

All HTTP probing targets loopback only and uses stdlib urllib so the default
install needs no extra dependency.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from grag.config import GragConfig, database_identity, derive_port

GRAG_HOME = Path.home() / ".grag"

_PROBE_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def run_dir() -> Path:
    return GRAG_HOME / "run"


def log_dir() -> Path:
    return GRAG_HOME / "logs"


def _identity8(db_path: Path) -> str:
    return database_identity(db_path)[:8]


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


def write_pidfile(db_path: Path, port: int) -> None:
    """Record this process as the server for db_path. Best-effort."""
    try:
        run_dir().mkdir(parents=True, exist_ok=True)
        pidfile_path(db_path).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": port,
                    "db": str(db_path.resolve()) if str(db_path) != ":memory:" else ":memory:",
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def remove_pidfile(db_path: Path) -> None:
    with contextlib.suppress(OSError):
        pidfile_path(db_path).unlink(missing_ok=True)


def read_pidfile(db_path: Path) -> dict | None:
    try:
        data = json.loads(pidfile_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "pid" in data else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
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


def probe_health(port: int) -> dict | None:
    """GET /api/health on loopback; None when unreachable/not grag."""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:
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


def find_server(db_path: Path) -> ServerInfo | None:
    """Locate a running server for db_path.

    Tries, in order: the port recorded in the pidfile, the derived
    per-database port, and the legacy default 8471. A server "matches" when
    its /api/health database_id equals db_path's identity.
    """
    expected = database_identity(db_path)
    pidinfo = read_pidfile(db_path)
    candidates: list[int] = []
    if pidinfo and isinstance(pidinfo.get("port"), int):
        candidates.append(pidinfo["port"])
    for port in (derive_port(db_path), 8471):
        if port not in candidates:
            candidates.append(port)
    for port in candidates:
        health = probe_health(port)
        if health is None:
            continue
        if health.get("database_id") == expected:
            pid = None
            if pidinfo and _pid_alive(int(pidinfo.get("pid", -1))):
                pid = int(pidinfo["pid"])
            return ServerInfo(
                port=port,
                version=health.get("version"),
                matches_db=True,
                pid=pid,
            )
    return None


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
    db = config.db_path
    lines = [f"database:  {db}"]
    if str(db) != ":memory:":
        resolved = db.resolve()
        if resolved.exists():
            lines.append(f"  exists:  yes ({_fmt_size(resolved.stat().st_size)})")
        else:
            lines.append("  exists:  no (created on first write)")
    lines.append(f"  id:      {_identity8(db)}")

    info = find_server(db)
    if info is not None:
        pid = f", pid {info.pid}" if info.pid else ""
        lines.append(
            f"server:    running on http://127.0.0.1:{info.port}/ "
            f"(grag {info.version or '?'}{pid})"
        )
        lines.append(f"  ui:      http://127.0.0.1:{info.port}/")
        lines.append(f"  mcp:     http://127.0.0.1:{info.port}/mcp")
    else:
        lines.append("server:    not running")
        stale = read_pidfile(db)
        if stale and not _pid_alive(int(stale.get("pid", -1))):
            remove_pidfile(db)
            lines.append("  (removed a stale pidfile from a previous run)")
    log = log_path(db)
    if log.exists():
        lines.append(f"log:       {log}")
    if config.embedder is not None:
        lines.append(
            f"embedder:  {config.embedder.provider} ({config.embedder.model})"
        )
    else:
        lines.append("embedder:  off — FTS-only retrieval (set GRAG_EMBED_PROVIDER)")
    return lines


def stop_server(db_path: Path) -> str:
    """SIGTERM the pidfile-registered server for db_path; returns a report."""
    info = read_pidfile(db_path)
    if info is None:
        return (
            "No pidfile for this database — nothing to stop.\n"
            "If a server is running it was started before pidfiles existed; "
            "stop that process manually and restart it via 'grag serve' or "
            "auto-serve."
        )
    pid = int(info.get("pid", -1))
    if not _pid_alive(pid):
        remove_pidfile(db_path)
        return "Server already gone (removed stale pidfile)."
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)
    if _pid_alive(pid):
        return f"Sent SIGTERM to pid {pid}; it has not exited yet."
    remove_pidfile(db_path)
    return f"Stopped server (pid {pid})."


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


def _repo_rows_http(port: int) -> list[dict] | None:
    """Repo staleness props via a running server; None if the query failed."""
    body = json.dumps(
        {
            "cypher": (
                "MATCH (r:Repo) RETURN r.path AS path, "
                "r.git_commit AS git_commit, r.ingested_at AS ingested_at"
            )
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/query",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
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
    info = find_server(config.db_path)
    if info is not None:
        repo_rows = _repo_rows_http(info.port)
    elif str(config.db_path) != ":memory:" and config.db_path.resolve().exists():
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
