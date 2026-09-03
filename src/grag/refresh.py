"""Keep the code graph in step with the working tree, automatically.

A serving process answers questions about a repository that keeps changing
underneath it. Before this module, freshness depended on someone remembering
to run ``ingest_code`` (``grag doctor`` would merely report "index is N
commits behind HEAD"). Now every retrieval call asks the ``CodeIndexRefresher``
first: at most once per ``interval`` it fingerprints each indexed checkout —
HEAD commit, the set of dirty/untracked files and their newest mtime — and
when a fingerprint moved it queues one incremental ``ingest_code`` on the
service's job thread. Incremental ingest rewrites only the files whose
content hash changed, so a one-file edit costs well under a second, and the
current request is answered immediately from the slightly stale graph while
the refresh lands for the next one (``SearchResponse.index_status ==
"refreshing"`` tells the caller).

Fingerprinting shells out to ``git`` (rev-parse + status --porcelain); paths
that are not git checkouts fall back to HEAD-less mtime tracking of nothing
— i.e. they are refreshed only by explicit ingest_code, as before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from grag.core.errors import GragError
from grag.core.types import CodeIngestRequest

if TYPE_CHECKING:
    from grag.service import GragService

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 5.0


@dataclass(frozen=True)
class Fingerprint:
    head: str  # HEAD commit sha ("" when unknown)
    tree: str  # digest of dirty/untracked paths + their newest mtime


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv over a local path
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def fingerprint(root: Path) -> Fingerprint | None:
    """Fingerprint of a checkout's indexable state; None outside git."""
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        return None
    status = _git(root, "status", "--porcelain", "--untracked-files=normal") or ""
    h = hashlib.sha256()
    newest = 0.0
    for line in status.splitlines():
        # "XY path" (or "XY old -> new" for renames): the path is what matters.
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        p = root / rel
        try:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
            elif p.is_dir():
                # Untracked directory: git lists it as one entry; walk it.
                for dirpath, _dirs, files in os.walk(p):
                    for name in files:
                        with suppress(OSError):
                            newest = max(newest, (Path(dirpath) / name).stat().st_mtime)
        except OSError:
            pass
    h.update(repr(newest).encode())
    return Fingerprint(head=head.strip(), tree=h.hexdigest())


class CodeIndexRefresher:
    """Throttled drift detector + background re-ingest for one GragService."""

    def __init__(self, service: GragService, *, interval: float = 30.0):
        self.service = service
        self.interval = max(1.0, float(interval))
        self._lock = threading.Lock()
        self._last_check = 0.0
        self._seen: dict[str, Fingerprint] = {}
        self._job_id: str | None = None
        self.refreshes = 0
        self.last_error: str | None = None

    # -- public --------------------------------------------------------------------

    def maybe_refresh(self) -> str | None:
        """Check for drift (throttled) and queue an incremental ingest if any.

        Returns "refreshing" while a refresh job is queued or running, else
        None. Never raises: a failed check is logged and retried next time.
        """
        with self._lock:
            now = time.monotonic()
            running = self._job_running()
            if now - self._last_check < self.interval:
                return "refreshing" if running else None
            self._last_check = now
            if running:
                return "refreshing"
            try:
                stale = self._stale_roots()
            except GragError as exc:  # no Repo table yet, etc.
                log.debug("auto-refresh check skipped: %s", exc)
                return None
            if not stale:
                return None
            try:
                job = self.service.submit_ingest_code(
                    CodeIngestRequest(paths=[str(p) for p in stale], incremental=True)
                )
            except GragError as exc:
                self.last_error = str(exc)
                log.warning("auto-refresh could not queue ingest: %s", exc)
                return None
            self._job_id = job.id
            self.refreshes += 1
            log.info("auto-refresh: re-indexing %d checkout(s): %s", len(stale), stale)
            return "refreshing"

    def status(self) -> dict:
        return {
            "refreshes": self.refreshes,
            "tracked": len(self._seen),
            "running": self._job_running(),
            "last_error": self.last_error,
        }

    # -- internals ------------------------------------------------------------------

    def _job_running(self) -> bool:
        if self._job_id is None:
            return False
        job = self.service.jobs.get(self._job_id)
        if job is None or job.status in ("done", "failed"):
            if job is not None and job.status == "failed":
                self.last_error = job.error
            self._job_id = None
            # Whatever the outcome, re-baseline so one bad tree state does
            # not re-queue on every interval.
            return False
        return True

    def _stale_roots(self) -> list[Path]:
        rows = self.service.engine.execute(
            "MATCH (r:Repo) RETURN r.path, r.git_commit"
        ).rows
        stale: list[Path] = []
        for path, commit in rows:
            if not path:
                continue
            root = Path(str(path))
            if not root.is_dir():
                continue
            fp = fingerprint(root)
            if fp is None:
                continue
            key = str(root)
            previous = self._seen.get(key)
            if previous is None:
                # First sight: the graph's recorded commit is the baseline,
                # and any dirty tree counts as drift we have never indexed.
                baseline_head = str(commit or "")
                moved = fp.head != baseline_head or fp.tree != _clean_tree_digest()
            else:
                moved = fp != previous
            self._seen[key] = fp
            if moved:
                stale.append(root)
        return stale


def _clean_tree_digest() -> str:
    """Digest a clean working tree produces (no dirty paths, mtime 0.0)."""
    h = hashlib.sha256()
    h.update(repr(0.0).encode())
    return h.hexdigest()
