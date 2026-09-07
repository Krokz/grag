"""Coalesced source verification and retryable code refresh off the read path."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from grag.code_state import Fingerprint as Fingerprint
from grag.code_state import fingerprint as fingerprint
from grag.code_state import index_records, saved_request, scan_sources
from grag.core.errors import FreshnessError, GragError, ShutdownError
from grag.core.limits import bounded_sources
from grag.core.types import FreshnessReport, FreshnessState, ReadPolicy

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class _RootState:
    options: str | None = None
    observed: str | None = None
    pending: str | None = None
    successful: str | None = None
    checked_at: str | None = None
    error: str | None = None
    unknown: bool = False
    failures: int = 0
    retry_at: float = 0.0


class CodeIndexRefresher:
    """One verification/refresh job per service, shared by concurrent readers.

    Checks and retry attempts are demand-driven. A read can serve stale data,
    wait to a deadline, or require verification before touching the graph.
    Filesystem/git checks never hold the reader's condition lock.
    """

    def __init__(
        self,
        service,
        *,
        interval: float = 30.0,
        retry_base: float = 1.0,
        retry_max: float = 300.0,
    ):
        self.service = service
        self.interval = max(0.0, float(interval))
        self.retry_base = max(0.01, retry_base)
        self.retry_max = max(self.retry_base, retry_max)
        self._condition = threading.Condition()
        self._roots: dict[str, _RootState] = {}
        self._checking = False
        self._running_root: str | None = None
        self._closed = False
        self._next_check = 0.0
        self._checked_at: str | None = None
        self._scan_error: str | None = None
        self._scan_failures = 0
        self._serial = 0
        self._completed = 0
        self._job_id: str | None = None
        self._rescan = False
        self.refreshes = 0

    @property
    def last_error(self) -> str | None:
        with self._condition:
            return self._scan_error or next(
                (r.error for r in self._roots.values() if r.error), None
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def invalidate(self) -> None:
        with self._condition:
            self._next_check = 0.0
            self._rescan = True
            self._condition.notify_all()

    def _report(self, *, timed_out: bool = False) -> FreshnessReport:
        state: FreshnessState
        if self._closed:
            state = "disabled"
        elif self._running_root is not None:
            state = "refreshing"
        elif self._scan_error or any(
            r.error and not r.unknown for r in self._roots.values()
        ):
            state = "error"
        elif any(r.unknown for r in self._roots.values()):
            state = "unknown"
        elif any(r.pending for r in self._roots.values()):
            state = "stale"
        elif self._checking:
            state = "checking"
        elif self._rescan:
            # An ingest can change the registered roots/options while a check
            # is running. Its earlier catalog snapshot cannot verify that work.
            state = "stale"
        elif self._checked_at is None:
            state = "unknown"
        else:
            state = "fresh"
        return FreshnessReport(
            status=state, checked_at=self._checked_at, timed_out=timed_out
        )

    def read(self, policy: ReadPolicy) -> FreshnessReport:
        deadline = time.monotonic() + policy.freshness_timeout_ms / 1000
        with self._condition:
            ticket = self._schedule(force=policy.freshness != "allow_stale")
            if policy.freshness == "allow_stale":
                return self._report()
            while True:
                report = self._report()
                if report.status == "fresh" and self._completed >= ticket:
                    return report
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    report = self._report(timed_out=remaining <= 0)
                    if policy.freshness == "require":
                        reason = self.last_error or report.status
                        raise FreshnessError(
                            f"Code index freshness could not be verified: {reason[:300]}",
                            freshness=report.model_dump(),
                            hint="Inspect GET /api/index/status. Resolve the reported issue, retry with a longer freshness_timeout_ms, or explicitly use freshness='allow_stale'.",
                        )
                    return report
                self._condition.wait(
                    timeout=min(
                        remaining, max(0.01, self._next_due() - time.monotonic())
                    )
                )
                if self._report().status != "fresh" or self._completed < ticket:
                    self._schedule(force=False)

    def maybe_refresh(self) -> str | None:
        report = self.read(ReadPolicy())
        return None if report.status == "fresh" else report.status

    def status(self, *, detail: bool = True) -> dict:
        with self._condition:
            out = {
                "freshness": self._report().model_dump(),
                "refreshes": self.refreshes,
                "tracked": len(self._roots),
                "running": self._checking and self._job_id is not None
                and self.service.get_job(self._job_id).status in ("queued", "running"),
                "job_id": self._job_id,
            }
            if detail:
                out["last_error"] = self.last_error
                out["roots"] = [
                    {
                        "path": path,
                        "observed_generation": r.observed,
                        "pending_generation": r.pending,
                        "successful_generation": r.successful,
                        "checked_at": r.checked_at,
                        "error": r.error,
                        "unknown": r.unknown,
                        "failures": r.failures,
                        "retry_in_s": max(0.0, r.retry_at - time.monotonic()),
                        "options": r.options,
                    }
                    for path, r in sorted(self._roots.items())
                ]
            return out

    def _next_due(self) -> float:
        if self._scan_error:
            return self._next_check
        due = [self._next_check]
        due.extend(
            r.retry_at
            for r in self._roots.values()
            if r.pending or (r.error and not r.unknown)
        )
        return min(due)

    def _schedule(self, *, force: bool) -> int:
        if self._checking or self._closed:
            return self._serial
        if self._scan_error and time.monotonic() < self._next_due():
            return self._serial
        if not force and time.monotonic() < self._next_due():
            return self._serial
        self._checking = True
        self._serial += 1
        serial = self._serial
        try:
            job = self.service.jobs.submit(
                "refresh_code", lambda: self._cycle(serial), {}
            )
            self._job_id = job.id
        except Exception as exc:  # noqa: BLE001 — preserve failures for retry/reporting
            self._checking = False
            self._scan_error = str(exc)
            self._scan_failures += 1
            self._next_check = time.monotonic() + self._backoff(self._scan_failures)
            log.warning("Could not queue code-index verification: %s", exc)
        return serial

    def _backoff(self, failures: int) -> float:
        return min(self.retry_max, self.retry_base * 2 ** min(max(0, failures - 1), 20))

    def _failed(self, state: _RootState, exc: Exception) -> None:
        with self._condition:
            state.error = str(exc)
            state.failures += 1
            state.retry_at = time.monotonic() + self._backoff(state.failures)
            self._condition.notify_all()

    @bounded_sources
    def _cycle(self, serial: int) -> dict:
        try:
            with self._condition:
                self._rescan = False
            if self._closed:
                raise ShutdownError()
            records = index_records(self.service.engine)
            with self._condition:
                self._roots = {p: self._roots.get(p, _RootState()) for p in records}
                self._scan_error = None
                self._scan_failures = 0
            for path, record in sorted(records.items()):
                if self._closed:
                    raise ShutdownError()
                self._check_root(path, record)
            if self._closed:
                raise ShutdownError()
            with self._condition:
                self._checked_at = _now()
                self._next_check = (
                    0.0 if self._rescan else time.monotonic() + self.interval
                )
        except ShutdownError:
            raise
        except Exception as exc:  # noqa: BLE001 — preserve failures for retry/reporting
            with self._condition:
                self._scan_error = str(exc)
                self._scan_failures += 1
                self._next_check = time.monotonic() + self._backoff(self._scan_failures)
            log.warning("Code-index verification failed: %s", exc)
        finally:
            with self._condition:
                self._completed = serial
                self._checking = False
                self._running_root = None
                self._condition.notify_all()
        return self.status(detail=False)

    def _check_root(self, path: str, record: dict) -> None:
        state = self._roots[path]
        encoded = record["_index_options"]
        with self._condition:
            if (
                state.options != encoded
                or state.successful != record["_index_generation"]
            ):
                state.failures = 0
                state.retry_at = 0.0
                state.error = None
            state.options = encoded
            state.successful = record["_index_generation"]
            state.unknown = not bool(encoded)
            if state.unknown:
                state.error = "Legacy index has no saved scope/options. Run ingest_code once with the intended paths and options."
                return
            if state.error and time.monotonic() < state.retry_at:
                return
        try:
            req = saved_request(Path(path), encoded)
            scan = scan_sources(Path(path), req)
            observed = scan.fingerprint.generation
            with self._condition:
                if state.observed != observed:
                    state.failures = 0
                    state.retry_at = 0.0
                    state.error = None
                state.observed = observed
                state.checked_at = _now()
                if observed == state.successful and not record["_index_error"]:
                    state.pending = None
                    state.error = None
                    state.failures = 0
                    state.retry_at = 0.0
                    return
                state.pending = observed
                if time.monotonic() < state.retry_at or self._closed:
                    return
                self._running_root = path
                self._condition.notify_all()
            try:
                with self.service.engine.code_ingest_lock:
                    latest = index_records(self.service.engine).get(path)
                    if self._closed or latest is None:
                        return
                    if latest["_index_options"] != encoded:
                        self.invalidate()
                        return
                    self.refreshes += 1
                    self.service.ingest_code(req)
                    after = index_records(self.service.engine)[path]
                    current = scan_sources(Path(path), req).fingerprint.generation
                with self._condition:
                    state.successful = after["_index_generation"]
                    state.observed = current
                    state.checked_at = _now()
                    state.pending = None if current == state.successful else current
                if after["_index_error"]:
                    raise GragError(after["_index_error"])
                with self._condition:
                    state.error = None
                    state.failures = 0
                    state.retry_at = time.monotonic() + 0.05 if state.pending else 0.0
            finally:
                with self._condition:
                    self._running_root = None
                    self._condition.notify_all()
        except ShutdownError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad root must not skip other roots
            self._failed(state, exc)
