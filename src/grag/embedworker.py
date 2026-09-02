"""Background embedding worker for serving processes.

Without a worker, grag embeds lazily on the request thread: an ingest embeds
its own writes before returning, and a search embeds up to
``config.max_embed_per_search`` pending nodes before answering. Both hold the
single write connection for as long as the embedder runs, so a large ingest
stalls every concurrent search behind the write lock.

A serving process (``grag serve``, ``grag mcp``) attaches one ``EmbedWorker``
per engine instead. Ingest and upsert paths then only *wake* the worker and
return immediately; the worker drains ``embedding IS NULL`` nodes in small
batches on its own thread, taking the write lock for one short statement per
node so interactive reads and writes interleave freely. Searches never embed
inline while a worker is attached (they still report ``pending_embeddings``,
which now shrinks on its own).

The worker is attached to the engine as ``engine.embed_worker`` so the
ingest/retrieval modules — whose signatures are frozen on (engine, config)
— can find it without a new parameter.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine

log = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 60.0


class EmbedWorker:
    def __init__(
        self,
        engine: Engine,
        config: GragConfig,
        *,
        batch_size: int = 32,
        idle_poll_seconds: float = 30.0,
    ):
        self.engine = engine
        self.config = config
        self.batch_size = max(1, int(batch_size))
        self.idle_poll_seconds = max(0.1, float(idle_poll_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.embedded_total = 0
        self.passes = 0
        self.last_error: str | None = None
        self.last_pass_at: float | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="grag-embed-worker", daemon=True
            )
            self._thread.start()
        # Drain whatever backlog the previous process left behind.
        self.wake()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        thread.join(timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def wake(self, table: str | None = None) -> None:
        """Signal that new un-embedded nodes may exist (any table)."""
        self._idle.clear()
        self._wake.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the worker has drained the backlog (tests, CLI drains)."""
        return self._idle.wait(timeout)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "idle": self._idle.is_set(),
            "embedded_total": self.embedded_total,
            "passes": self.passes,
            "last_error": self.last_error,
        }

    # -- work ---------------------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._wake.wait(timeout=self.idle_poll_seconds)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.drain_once()
            except Exception as exc:  # noqa: BLE001 — keep the worker alive
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Embedding worker pass failed (retrying): %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                self._wake.set()  # retry the pass after the backoff
                continue
            backoff = 1.0
            if not self._wake.is_set():
                self._idle.set()

    def drain_once(self) -> int:
        """Embed every pending node across searchable tables; return the count.

        Runs on the worker thread normally; callable directly for a
        synchronous drain. Batches are small so each write-lock hold is
        short and a concurrent ingest or search slots in between them.
        """
        from grag.retrieval.vectors import embed_pending_nodes, searchable_node_tables

        if self.config.embedder is None:
            return 0
        total = 0
        for table in searchable_node_tables(self.engine, self.config):
            while not self._stop.is_set():
                n = embed_pending_nodes(
                    self.engine,
                    self.config,
                    table,
                    batch_size=self.batch_size,
                    max_nodes=self.batch_size,
                )
                total += n
                if n < self.batch_size:
                    break
                # Yield between batches so request threads waiting on the
                # write lock (and the GIL) get a turn.
                time.sleep(0)
        self.embedded_total += total
        self.passes += 1
        self.last_pass_at = time.time()
        self.last_error = None
        return total


def attached_worker(engine: Engine) -> EmbedWorker | None:
    """The EmbedWorker serving `engine`, if a serving process attached one."""
    worker = getattr(engine, "embed_worker", None)
    return worker if isinstance(worker, EmbedWorker) and worker.running else None


def notify_embed_worker(engine: Engine, table: str | None = None) -> bool:
    """Wake the attached worker; False when none is attached (embed inline)."""
    worker = attached_worker(engine)
    if worker is None:
        return False
    worker.wake(table)
    return True
