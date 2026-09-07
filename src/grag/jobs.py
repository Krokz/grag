"""In-process background jobs for long-running ingests.

A code or document ingest over a large tree can take minutes; holding an HTTP
request (or an MCP tool call) open that long times out clients and, on a
shared server, ties a CI hook to the ingest's wall-clock. ``JobManager`` runs
ingests on a single worker thread per database and hands the caller a job id
to poll instead.

One worker thread on purpose: ingests serialize on the engine's write lock
anyway, so running two concurrently only interleaves their lock holds. The
queue also means a CI hook that fires twice in quick succession simply runs
twice in order — never two writers fighting.

Records are in memory only (bounded history); a restart forgets finished
jobs, which is fine because the ingest's effect — the graph — is durable.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from grag.core.errors import GragError, ShutdownError
from grag.core.types import JobRecord

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    def __init__(self, *, max_history: int = 200):
        self.max_history = max(1, int(max_history))
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grag-job")
        self._closed = False
        self._futures: dict[str, Future] = {}

    def submit(
        self, kind: str, fn: Callable[[], BaseModel | dict[str, Any]], params: dict
    ) -> JobRecord:
        job = JobRecord(id=secrets.token_hex(8), kind=kind, created_at=_now(), params=params)
        with self._lock:
            if self._closed:
                raise ShutdownError()
            self._jobs[job.id] = job
            while len(self._jobs) > self.max_history:
                _oldest_id, oldest = next(iter(self._jobs.items()))
                if oldest.status in ("queued", "running"):
                    break  # never forget live work
                self._jobs.popitem(last=False)
            # Admission and executor submission share the shutdown lock. Otherwise
            # shutdown can strand a queued record before submit reaches the pool.
            try:
                future = self._pool.submit(self._run, job.id, fn)
            except RuntimeError as exc:
                self._update(job.id, status="failed", finished_at=_now(), error=str(exc))
                raise GragError("The job could not be submitted.", hint=str(exc)) from exc
            self._futures[job.id] = future
            future.add_done_callback(lambda done: self._finished(job.id, done))
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy() if job is not None else None

    def list(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.reverse()  # newest first
        return [j.model_copy() for j in jobs[: max(1, int(limit))]]

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            for future in list(self._futures.values()):
                future.cancel()  # running work drains; only queued work is cancelled
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def shutdown_status(self) -> dict:
        with self._lock:
            return {
                "active": [j.id for j in self._jobs.values() if j.status in ("queued", "running")],
                "cancelled": [j.id for j in self._jobs.values() if j.status == "cancelled"],
                "failed": [j.id for j in self._jobs.values() if j.status == "failed"],
            }

    def _finished(self, job_id: str, future: Future) -> None:
        with self._lock:
            if future.cancelled():
                self._update(job_id, status="cancelled", finished_at=_now(),
                             error="Cancelled before execution during server shutdown.")
            elif (error := future.exception()) is not None:
                # Includes SystemExit/KeyboardInterrupt escaping the callable.
                self._update(job_id, status="failed", finished_at=_now(),
                             error=f"{type(error).__name__}: {error}")
            self._futures.pop(job_id, None)

    # -- internals ------------------------------------------------------------------

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._jobs[job_id] = job.model_copy(update=fields)

    def _run(self, job_id: str, fn: Callable[[], BaseModel | dict[str, Any]]) -> None:
        self._update(job_id, status="running", started_at=_now())
        try:
            out = fn()
            result = out.model_dump() if isinstance(out, BaseModel) else dict(out)
        except ShutdownError as exc:
            self._update(job_id, status="cancelled", finished_at=_now(), error=exc.message)
            return
        except GragError as exc:
            message = exc.message + (f" ({exc.hint})" if exc.hint else "")
            self._update(job_id, status="failed", finished_at=_now(), error=message)
            return
        except Exception as exc:
            log.exception("Background job %s failed", job_id)
            self._update(
                job_id,
                status="failed",
                finished_at=_now(),
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._update(job_id, status="done", finished_at=_now(), result=result)
