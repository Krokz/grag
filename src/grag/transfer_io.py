"""Stage, validate and atomically publish backups and restored databases."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import httpx2

from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.transfer import (
    MAX_RECORD_BYTES,
    _copy,
    _error,
    capture_snapshot,
    read_archive,
    restore_archive,
    verify_contents,
)

_SIDECARS = ("", ".wal", ".shadow", ".wal.checkpoint", ".checkpoint", ".ses")


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _safe_output(path: Path, database: Path) -> None:
    for suffix in _SIDECARS:
        source = Path(str(database) + suffix)
        if path.resolve() == source.resolve() or (
            path.exists() and source.exists() and os.path.samefile(path, source)
        ):
            raise _error(
                "Backup output must not replace the database or one of its sidecars"
            )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _error("Backup output must be a regular file, not a link or directory")


@contextmanager
def atomic_output(path: str | None, *, database: Path) -> Iterator[TextIO]:
    """Even stdout is withheld until a complete verified export is available."""
    destination = Path(path).expanduser().absolute() if path else None
    if destination:
        _safe_output(destination, database)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.partial-"
        if destination
        else "grag-export-partial-",
        dir=destination.parent if destination else None,
    )
    staged = Path(name)
    try:
        with os.fdopen(fd, "w+", encoding="utf-8", newline="\n") as stream:
            yield stream
            stream.flush()
            stream.seek(0)
            with read_archive(_limited_lines(stream)):
                pass
            stream.flush()
            os.fsync(stream.fileno())
            if destination is None:
                stream.seek(0)
                binary = getattr(sys.stdout, "buffer", None)
                if binary is None:
                    shutil.copyfileobj(stream, sys.stdout)
                else:
                    # Preserve exact UTF-8/LF checksum bytes even with a legacy
                    # console codec or Windows text newline translation.
                    sys.stdout.flush()
                    while chunk := stream.read(64 * 1024):
                        binary.write(chunk.encode("utf-8"))
                    binary.flush()
        if destination:
            _safe_output(destination, database)
            os.replace(staged, destination)
            _sync_dir(destination.parent)
    finally:
        staged.unlink(missing_ok=True)


def _limited_lines(stream: TextIO) -> Iterator[str]:
    while line := stream.readline(MAX_RECORD_BYTES + 2):
        yield line


def export_client(client: GraphClient, path: str | None) -> int:
    with atomic_output(path, database=client.config.db_path) as sink:
        if client.service is not None:
            with (
                client.service.operation(),
                capture_snapshot(client.service.engine) as snapshot,
            ):
                return _copy(snapshot, sink)
        client._verify_owner()
        if client.capabilities.get("snapshot_format", 0) < 2:
            raise _error(
                "The running server cannot produce a verified v2 snapshot. Restart it with the updated grag installation"
            )
        if client.http is None:
            raise _error("Graph client is not open")
        try:
            with client.http.stream("GET", "/api/export", timeout=600) as response:
                if response.status_code != 200:
                    raise _error(
                        f"Export server returned HTTP {response.status_code}; inspect its log and selected database"
                    )
                # Bounded decoding avoids an unbounded iter_lines buffer from a
                # broken server. Validation is done before publication/stdout.
                import codecs

                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                count = 0
                for data in response.iter_bytes(chunk_size=64 * 1024):
                    text = decoder.decode(data)
                    sink.write(text)
                    count += text.count("\n")
                sink.write(decoder.decode(b"", final=True))
                return count
        except (httpx2.HTTPError, UnicodeError) as exc:
            raise _error(
                "Export download failed; no completed backup was published"
            ) from exc


def download(
    server_url: str,
    out_path: str | None,
    *,
    api_token: str | None = None,
    db_name: str | None = None,
    allow_insecure: bool = False,
) -> int:
    config = GragConfig(
        server_url=server_url,
        api_token=api_token,
        server_db=db_name,
        allow_insecure_http=allow_insecure,
    )
    with GraphClient(config) as client:
        return export_client(client, out_path)


def restore_file(
    config: GragConfig, source: str, *, allow_legacy: bool = False
) -> dict:
    """Publish only a checkpointed, strictly reopened and content-verified copy.

    The destination must be new. Hard-link publication is atomic and refuses
    overwrite races; staging lives on the same filesystem and is removed after
    success. Abrupt process death leaves an identifiable .grag-restore-* directory.
    """
    if config.server_url or config.db_dir or str(config.db_path) == ":memory:":
        raise _error(
            "Restore needs a new local file selected with --db; server/database-directory targets are not supported"
        )
    target = config.db_path.expanduser().absolute()
    if any(os.path.lexists(str(target) + suffix) for suffix in _SIDECARS):
        raise _error(
            "Restore destination or sidecar already exists; choose a new --db file"
        )
    with (
        open(source, encoding="utf-8", newline="") as input_file,
        read_archive(_limited_lines(input_file), allow_legacy=allow_legacy) as archive,
    ):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix=".grag-restore-", dir=target.parent
        ) as work:
            root = Path(work)
            staged = root / "restored.lbdb"
            # Separate source validation from target creation, so malformed input
            # never creates a target DB. No background embedding/refresh worker.
            cfg = config.model_copy(update={"db_path": staged, "embedder": None})
            with Engine(cfg) as engine:
                report = restore_archive(engine, archive)
                engine.execute_write("CHECKPOINT")  # explicit, never suppress failure
            with Engine(cfg, read_only=True) as check:
                verify_contents(check, archive)
            if any(
                Path(str(staged) + suffix).exists()
                and Path(str(staged) + suffix).stat().st_size
                for suffix in _SIDECARS[1:-1]
            ):
                raise _error(
                    "Restored copy still needs replay sidecars after checkpoint; refusing publication"
                )
            # Windows' file-buffer flush requires a writable handle. This is
            # our verified staging copy; r+b neither truncates nor changes it.
            with staged.open("r+b") as file:
                os.fsync(file.fileno())
            if any(os.path.lexists(str(target) + suffix) for suffix in _SIDECARS):
                raise _error(
                    "Restore destination appeared during verification; it was not replaced"
                )
            try:
                os.link(staged, target)
            except OSError as exc:
                raise _error(
                    f"Could not publish the verified restore without overwriting a file: {exc}"
                ) from exc
            _sync_dir(target.parent)
            return {**report, "database": str(target), "reopen_verified": True}
