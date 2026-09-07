"""Offline recovery into a separate, verified copy. Never opens the source DB.

Snapshotting runs in a child process: POSIX record locks are process-owned,
so probing a file held by an Engine in this process could release its lock.
Native replay also runs in children to contain crashes from damaged files.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grag.config import GragConfig
from grag.core.errors import GragError

# Preserve checkpoint artifacts too; never infer that a WAL is disposable
# merely because a shadow/checkpoint companion is absent or empty.
_SUFFIXES = ("", ".wal", ".shadow", ".wal.checkpoint", ".checkpoint")
_CHUNK = 1024 * 1024


class RecoveryError(GragError):
    """Recovery was refused or its candidate did not pass verification."""


def is_replay_error(message: str) -> bool:
    message = message.lower()
    if "could not set lock" in message or "permission denied" in message:
        return False
    # Reproduced on 0.20.2 when replaying ALTER ... DEFAULT NULL after commit.
    # This native opening error omits the word WAL; keep normal opens strict
    # and route it to the same preserve-and-recover-a-copy guidance.
    if "trying to a create a vector with any type" in message:
        return True
    return bool(re.search(r"\b(wal|shadow)\b", message)) and any(
        word in message for word in ("replay", "corrupt", "checksum")
    )


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(_CHUNK):
            digest.update(data)
    return digest.hexdigest()


def _copy_fd(fd: int, destination: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    out = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(out, "wb") as stream:
        while data := os.read(fd, _CHUNK):
            stream.write(data)
            digest.update(data)
            size += len(data)
        stream.flush()
        os.fsync(stream.fileno())
    if _digest(destination) != digest.hexdigest():
        raise RecoveryError(f"Snapshot checksum verification failed: {destination}")
    return {"size": size, "sha256": digest.hexdigest()}


def _file_state(path: Path) -> tuple | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError(f"Recovery requires regular files, not links/directories: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_exclusive_windows(path: Path) -> int:
    if sys.platform != "win32":
        raise RecoveryError("Windows snapshot handles require Windows.")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # Read access, no sharing, OPEN_EXISTING. An existing native DB handle
    # prevents this open; while held it prevents a new writer from opening.
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise


@contextmanager
def _locked_source(path: Path):
    """Read lock excluding native writers; only call in the snapshot child."""
    fd = -1
    try:
        if os.name == "nt":
            fd = _open_exclusive_windows(path)
        else:
            import fcntl

            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            # Ladybug uses fcntl F_SETLK; lockf uses the same lock family.
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise RecoveryError(
            f"Cannot lock database for an offline snapshot: {path}: {exc}",
            hint="Stop every server/client or supervisor using this file, then retry.",
        ) from exc
    try:
        yield fd
    finally:
        os.close(fd)


def _snapshot(source: Path, destination: Path) -> dict:
    paths = {suffix: Path(f"{source}{suffix}") for suffix in _SUFFIXES}
    with _locked_source(source) as db_fd:
        before = {suffix: _file_state(path) for suffix, path in paths.items()}
        if before[""] is None:
            raise RecoveryError(f"Database disappeared before snapshot: {source}")
        descriptor = os.fstat(db_fd)
        if (descriptor.st_dev, descriptor.st_ino) != before[""][:2]:
            raise RecoveryError("Database was replaced while acquiring its snapshot lock.")
        files = {}
        for suffix, path in paths.items():
            expected_state = before[suffix]
            if expected_state is None:
                continue
            target = destination / f"database.lbdb{suffix}"
            if suffix == "":
                files[suffix] = _copy_fd(db_fd, target)
            else:
                fd = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                )
                try:
                    files[suffix] = _copy_fd(fd, target)
                finally:
                    os.close(fd)
            if files[suffix]["size"] != expected_state[2]:
                raise RecoveryError(f"Source length changed or snapshot read was incomplete: {path}")
        if before != {suffix: _file_state(path) for suffix, path in paths.items()}:
            raise RecoveryError("Database files changed during snapshot; no recovery was attempted.")
        _sync_dir(destination)
        return {"files": files}


def _restore_snapshot(bundle: Path, files: dict, attempt: str) -> Path:
    target = bundle / attempt
    target.mkdir(mode=0o700)
    for suffix, expected in files.items():
        source = bundle / "original" / f"database.lbdb{suffix}"
        if _digest(source) != expected["sha256"] or source.stat().st_size != expected["size"]:
            raise RecoveryError(f"Preserved snapshot failed verification: {source}")
        with source.open("rb") as stream:
            actual = _copy_fd(stream.fileno(), target / f"recovered.lbdb{suffix}")
        if actual != expected:
            raise RecoveryError("Snapshot changed while preparing a recovery attempt.")
    _sync_dir(target)
    _sync_dir(bundle)
    return target / "recovered.lbdb"


def _inspect(engine: Any) -> dict[str, dict[str, Any]]:
    from grag.core.ident import validate_identifier

    tables = {}
    for _, name, kind, *_ in engine.execute("CALL SHOW_TABLES() RETURN *").rows:
        validate_identifier(name)
        if str(kind).upper() == "NODE":
            match, value = f"MATCH (n:{name})", "n"
        elif str(kind).upper() == "REL":
            match, value = f"MATCH ()-[r:{name}]->()", "r"
        else:
            raise RecoveryError(f"Cannot verify unsupported table kind {kind}: {name}")
        count = engine.execute(f"{match} RETURN count({value})").rows[0][0]
        # Exercise property decoding as well as structural row counts.
        engine.execute(f"{match} RETURN {value} LIMIT 1")
        tables[name] = {"kind": str(kind).upper(), "rows": count}
    return tables


def _replay(path: Path, buffer_pool_size: int, *, allow_loss: bool) -> dict:
    from grag.core.engine import Engine

    config = GragConfig(db_path=path, embedder=None, buffer_pool_size=buffer_pool_size)
    with Engine(config, _recover_wal=allow_loss) as engine:
        tables = _inspect(engine)
        engine.execute_write("CHECKPOINT")
    # A strict reopen must succeed without fallback and retain all observed
    # node/relationship counts. This does not establish application semantics.
    with Engine(config) as engine:
        if _inspect(engine) != tables:
            raise RecoveryError("Recovered table counts changed after checkpoint/reopen.")
        engine.execute_write("CHECKPOINT")
    return {"tables": tables, "database_sha256": _digest(path)}


def _worker(args: list[str], *, timeout: float) -> dict:
    try:
        result = subprocess.run(  # noqa: S603 — fixed module, argv paths, no shell
            [sys.executable, "-m", "grag.recovery", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError("Recovery worker timed out; preserved inputs remain available.") from exc
    try:
        report = json.loads(result.stdout)
    except ValueError:
        report = {"ok": False, "error": f"Recovery worker exited with code {result.returncode}. "
                  f"{result.stderr[-2000:]}"}
    if result.returncode != 0:
        report["ok"] = False
    return report


def recover_database(
    config: GragConfig, *, out_dir: Path | None = None,
    allow_data_loss: bool = False, timeout: float = 300,
) -> dict:
    """Preserve offline inputs, replay a copy, and return a durable manifest.

    An existing output directory is never reused. Failed attempts and raw
    snapshots remain available; callers must not treat them as verified DBs.
    """
    import grag

    if config.db_dir is not None or str(config.db_path) == ":memory:":
        raise RecoveryError("Recovery requires one explicit on-disk database (--db <file>).")
    if timeout <= 0:
        raise RecoveryError("Recovery timeout must be positive.")
    source = config.db_path.expanduser().absolute()
    if _file_state(source) is None:
        raise RecoveryError(f"Database does not exist: {source}")
    source = source.resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = (out_dir.expanduser().absolute() if out_dir else
              source.with_name(f"{source.name}.recovery-{timestamp}-{uuid.uuid4().hex[:8]}"))
    bundle.mkdir(mode=0o700)  # never overwrite or reuse another recovery attempt
    (bundle / "original").mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "format": 1, "source": str(source), "bundle": str(bundle),
        "created_at": timestamp, "status": "snapshotting", "source_modified": False,
        "allow_data_loss": allow_data_loss, "data_loss_possible": False,
        "runtime": {"grag": grag.__version__, "ladybug": importlib.metadata.version("ladybug"),
                    "python": sys.version.split()[0], "platform": sys.platform},
    }
    manifest_path = bundle / "manifest.json"
    _write_json(manifest_path, manifest)
    try:
        snapshot = _worker(["snapshot", str(source), str(bundle / "original")], timeout=timeout)
        if not snapshot.get("ok"):
            raise RecoveryError(snapshot.get("error", "Snapshot failed."))
        manifest.update(files=snapshot["files"], status="snapshot_complete")
        _write_json(manifest_path, manifest)  # durable before native replay begins
        path = _restore_snapshot(bundle, manifest["files"], "strict")
        report = _worker(["replay", str(path), str(config.buffer_pool_size), "strict"], timeout=timeout)
        if not report.get("ok"):
            manifest["strict_error"] = report.get("error", "Strict recovery failed.")
            if not report.get("replay_error") or not allow_data_loss:
                raise RecoveryError(
                    manifest["strict_error"],
                    hint="Inspect the preserved snapshot. For WAL replay failure only, retry recover "
                    "with --allow-data-loss to permit partial replay into a new copy. "
                    "Committed writes may be lost; the amount cannot be determined automatically.",
                )
            manifest.update(status="partial_replay", data_loss_possible=True)
            _write_json(manifest_path, manifest)
            # Never retry the copy a failed replay may already have modified.
            path = _restore_snapshot(bundle, manifest["files"], "partial")
            report = _worker(["replay", str(path), str(config.buffer_pool_size), "partial"], timeout=timeout)
            if not report.get("ok"):
                raise RecoveryError(report.get("error", "Partial recovery failed."))
        manifest.update(status="verified", recovered_db=str(path), **{
            key: report[key] for key in ("tables", "database_sha256")
        })
        manifest["verification"] = "strict reopen, checkpoint, table counts and sample property reads"
        _write_json(manifest_path, manifest)
        return manifest
    except BaseException as exc:
        manifest.update(status="failed", error=str(exc))
        _write_json(manifest_path, manifest)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RecoveryError(
            f"Recovery did not complete: {exc}",
            hint=f"Original files were not opened for writes. Preserve {bundle}; see {manifest_path}.",
        ) from exc


def _main() -> int:
    try:
        if sys.argv[1] == "snapshot":
            result = _snapshot(Path(sys.argv[2]), Path(sys.argv[3]))
        else:
            result = _replay(Path(sys.argv[2]), int(sys.argv[3]), allow_loss=sys.argv[4] == "partial")
        print(json.dumps({"ok": True, **result}))
        return 0
    except Exception as exc:  # noqa: BLE001 — cross-process error protocol
        replay_error = is_replay_error(str(exc))
        # The parent already explains the recovery workflow. Retain the
        # native error without repeating normal-open recovery hints.
        message = exc.message if replay_error and isinstance(exc, GragError) else str(exc)
        print(json.dumps({"ok": False, "error": message, "replay_error": replay_error}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
