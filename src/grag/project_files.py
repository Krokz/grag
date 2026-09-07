"""Reviewable init file plans and atomic publication with preserved originals.

Atomicity is per file, not across a client config, instructions and skills. All
originals are backed up and all writes staged before the first publication.
The manifest records old/new hashes so an interrupted multi-file run is auditable.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from grag.core.errors import ConfigurationError


class ProjectConfigError(ConfigurationError):
    """An init plan could not be read or applied safely."""


@dataclass(frozen=True)
class Snapshot:
    data: bytes | None
    mode: int = 0o600
    identity: tuple[int, int] | None = None
    location: Path | None = None

    @property
    def text(self) -> str:
        return self.data.decode("utf-8") if self.data is not None else ""


def snapshot(path: Path) -> Snapshot:
    """Read once; only a genuinely missing path is a new file.

    Refuse links and nonregular files: replacing their directory entry would
    change the meaning of a shared dotfile rather than update it in place.
    """
    info = None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProjectConfigError(
                f"{path}: expected a regular file with no links; left intact"
            )
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ProjectConfigError(f"{path}: changed while reading; retry init")
            data = stream.read()
            after = os.fstat(stream.fileno())

        def version(value: os.stat_result, *, cross_api: bool = False) -> tuple:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mtime_ns,
                # CPython's Windows lstat reports creation time here, while
                # fstat can report metadata-change time. Compare each API's
                # timestamp before/after, but not against the other API.
                None if cross_api and sys.platform == "win32" else value.st_ctime_ns,
                value.st_size,
                value.st_mode,
                value.st_nlink,
                value.st_uid,
            )

        if (
            version(info) != version(path.lstat())
            or version(opened) != version(after)
            or version(info, cross_api=True) != version(opened, cross_api=True)
        ):
            raise ProjectConfigError(f"{path}: changed while reading; retry init")
        if os.name != "nt" and info.st_uid != os.getuid():
            raise ProjectConfigError(
                f"{path}: file is owned by another user; left intact"
            )
        data.decode("utf-8")  # validate without universal-newline conversion
        return Snapshot(
            data, stat.S_IMODE(info.st_mode), (info.st_dev, info.st_ino), path.resolve()
        )
    except FileNotFoundError:
        # A dangling symlink or a disappearing file is not a new config.
        if path.is_symlink() or info is not None:
            raise ProjectConfigError(
                f"{path}: changed while reading; retry init"
            ) from None
        return Snapshot(None, location=path.resolve())
    except (OSError, UnicodeError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) else "not valid UTF-8"
        raise ProjectConfigError(
            f"{path}: cannot read configuration ({detail}); left intact"
        ) from exc


@dataclass(frozen=True)
class WriteOp:
    path: Path
    content: str
    before: Snapshot

    @property
    def created(self) -> bool:
        return self.before.data is None


class SkipOp(NamedTuple):
    path: Path
    reason: str
    snippet: str = ""


@dataclass(frozen=True)
class DeleteOp:
    path: Path
    before: Snapshot


FileOp = WriteOp | DeleteOp
Op = FileOp | SkipOp


def _changed(op: FileOp) -> bool:
    return isinstance(op, DeleteOp) or op.content.encode("utf-8") != op.before.data


def _check(op: FileOp) -> None:
    if snapshot(op.path) != op.before:
        raise ProjectConfigError(
            f"{op.path}: changed since planning; retry init to review a fresh plan"
        )


def preview_ops(ops: list[Op]) -> None:
    """Show actual edits without creating directories, backups or lock files."""
    print("Proposed changes (dry run):")
    for op in ops:
        if isinstance(op, SkipOp):
            _skip(op)
            continue
        _check(op)
        if not _changed(op):
            print(f"  unchanged: {op.path}")
            continue
        after = op.content if isinstance(op, WriteOp) else ""
        for line in difflib.unified_diff(
            op.before.text.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(op.path) if op.before.data is not None else "/dev/null",
            tofile=str(op.path) if isinstance(op, WriteOp) else "/dev/null",
            n=0,
        ):
            print(line, end="")
            if not line.endswith("\n"):
                print("\n\\ No newline at end of file")


def _skip(op: SkipOp) -> None:
    print(f"  skip: {op.path} ({op.reason})")
    if op.snippet:
        print(op.snippet)


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mkdirs(path: Path) -> None:
    """Persist new directory entries before relying on them for backups/files."""
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        _mkdirs(path.parent)
        _mkdirs(path)
        return
    except FileExistsError:
        if not path.is_dir():
            raise
        return
    _sync_dir(path)
    _sync_dir(path.parent)


def _private_dir(path: Path) -> None:
    _mkdirs(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        os.name != "nt" and info.st_uid != os.getuid()
    ):
        raise ProjectConfigError(
            f"{path}: backup directory must be owned by this user and not a link"
        )
    path.chmod(0o700)


@contextmanager
def _init_lock(root: Path):
    """Serialize grag init writers, including different projects' user configs."""
    _private_dir(root)
    fd = os.open(
        root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProjectConfigError(
                "Another grag init is applying changes; retry when it finishes"
            ) from exc
        yield
    finally:
        os.close(fd)


def _write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    with path.open("xb") as stream:
        # Apply permissions before secret bytes reach disk.
        path.chmod(mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _stage(op: WriteOp) -> Path:
    _mkdirs(op.path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{op.path.name}.grag-", dir=op.path.parent)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            path.chmod(op.before.mode)
            stream.write(op.content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def apply_ops(ops: list[Op]) -> Path | None:
    """Validate all targets, preserve originals, stage, then publish per file.

    Rechecks detect stale plans, including concurrent grag init runs. External
    editors do not honor our lock: close them during apply; filesystem rename
    has no portable compare-and-swap against another process's final write.
    """
    pending: list[FileOp] = []
    seen: set[Path] = set()
    for op in ops:
        if isinstance(op, SkipOp):
            _skip(op)
            continue
        _check(op)
        target = op.path.resolve()
        if target in seen:
            raise ProjectConfigError(f"{op.path}: duplicate destination in init plan")
        seen.add(target)
        if _changed(op):
            pending.append(op)
        else:
            print(f"  unchanged: {op.path}")
    if not pending:
        return None

    root = Path.home() / ".grag" / "backups" / "init"
    bundle: Path | None = None
    prepared = False
    staged: dict[Path, Path] = {}
    completed: list[str] = []
    try:
        with _init_lock(root):
            for op in pending:
                _check(op)
            bundle = Path(tempfile.mkdtemp(prefix="run-", dir=root))
            entries = []
            for index, op in enumerate(pending):
                before = op.before.data
                backup = f"{index:03d}.original" if before is not None else None
                if backup is not None and before is not None:
                    _write_file(bundle / backup, before)
                after = op.content.encode("utf-8") if isinstance(op, WriteOp) else None
                entries.append(
                    {
                        "path": str(op.path.absolute()),
                        "backup": backup,
                        "mode": op.before.mode,
                        "before_sha256": hashlib.sha256(before).hexdigest()
                        if before is not None
                        else None,
                        "after_sha256": hashlib.sha256(after).hexdigest()
                        if after is not None
                        else None,
                        "action": "delete"
                        if isinstance(op, DeleteOp)
                        else "create"
                        if op.created
                        else "update",
                    }
                )
            _write_file(
                bundle / "manifest.json",
                (
                    json.dumps(
                        {
                            "version": 1,
                            "state": "prepared",
                            "files": entries,
                            "note": "Each file publishes atomically. Compare hashes to identify a partially applied run.",
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
            )
            _sync_dir(bundle)
            _sync_dir(root)
            prepared = True
            print(f"  Originals and change manifest: {bundle}")
            for op in pending:
                if isinstance(op, WriteOp):
                    staged[op.path] = _stage(op)
            for op in pending:
                _check(op)
            for op in pending:
                _check(op)
                if isinstance(op, DeleteOp):
                    op.path.unlink()
                    verb = "delete"
                elif op.created:
                    # Publish without clobbering a file created after the recheck.
                    os.link(staged[op.path], op.path)
                    verb = "create"
                else:
                    os.replace(staged[op.path], op.path)
                    verb = "update"
                completed.append(str(op.path))
                if isinstance(op, WriteOp) and op.created:
                    staged[op.path].unlink()
                _sync_dir(op.path.parent)
                print(f"  {verb}: {op.path}")
            _write_file(bundle / "complete.json", b'{"state": "complete"}\n')
            _sync_dir(bundle)
    except (OSError, ProjectConfigError, UnicodeError) as exc:
        hint = ""
        if bundle is not None:
            label = (
                "Preserved originals/manifest"
                if prepared
                else "Incomplete backup attempt"
            )
            hint = f" {label}: {bundle}."
        raise ProjectConfigError(
            f"Init stopped: {exc}. Published {len(completed)} file(s).{hint} "
            "Review the files before retrying; an interrupted multi-file run may be partially applied."
        ) from exc
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
    return bundle
