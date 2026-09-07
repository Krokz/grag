"""LadybugDB engine wrapper — the only module in grag that imports ladybug.

Threading model: embedded LadybugDB is single-writer. Writes go through
execute_write() (serialized on one connection); reads borrow pooled
connections. write_transaction() holds that writer across a complete DML
operation and routes same-thread reads through it; other readers keep seeing
committed data. All values returned to callers are plain python objects; raw
node/rel/path values are converted to contract models by the helpers below.

Verified LadybugDB value formats (see tests/test_engine_smoke.py):
    node: {"_ID": {"table": int, "offset": int}, "_LABEL": str, <props...>}
    rel:  {"_ID": {...}, "_SRC": {...}, "_DST": {...}, "_LABEL": str, <props...>}
    path: {"_NODES": [node...], "_RELS": [rel...]}
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ladybug as lb

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, CypherError, GragError
from grag.core.ident import validate_identifier
from grag.core.limits import charge, charge_result
from grag.core.types import (
    EMB_CODE_PROP,
    EMB_FINGERPRINT_PROP,
    EMBEDDING_PROP,
    RESERVED_PREFIX,
    EdgeRecord,
    NodeRecord,
    SchemaDocument,
    Subgraph,
    make_node_id,
)

logger = logging.getLogger("grag")

# Never exposed inside NodeRecord.properties: internal identifiers and bulky
# vector payloads (retrieval reads vectors via explicit Cypher projections).
_HIDDEN_NODE_PROPS = {
    "_ID", "_LABEL", EMBEDDING_PROP, EMB_CODE_PROP, EMB_FINGERPRINT_PROP,
    "_index_options", "_index_generation", "_index_error",
}
_HIDDEN_REL_PROPS = {"_ID", "_LABEL", "_SRC", "_DST", "_document_owner"}


@dataclass
class EngineResult:
    columns: list[str]
    rows: list[list[Any]]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


class Engine:
    def __init__(self, config: GragConfig, *, _recover_wal: bool = False, read_only: bool = False):
        self.config = config
        self.read_only = read_only
        if read_only and (_recover_wal or not Path(config.db_path).is_file()):
            raise ConfigurationError("Read-only inspection requires an existing database file")
        # Private recovery-worker option: only used on a disposable copy whose
        # original DB/WAL/shadow files have already been preserved.
        self.wal_recovered = _recover_wal
        db_path = str(config.db_path)
        self._db_path: Path | None = None
        if db_path != ":memory:":
            self._db_path = Path(db_path)
            parent = self._db_path.parent
            parent_existed = parent.exists()
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed and os.name != "nt":
                parent.chmod(0o700)
        self._db = self._open_db(db_path, config)
        if not read_only:
            self._secure_database_files()
        self._write_conn = lb.Connection(self._db)
        self._write_lock = threading.RLock()
        # Conservative invalidation: any writer statement (including failed
        # statements) discards schema views. Readers never cache transactions.
        self.schema_cache: dict[str, SchemaDocument] = {}
        self._catalog_generation = 0
        self._prepared_catalog: dict[lb.Connection, int] = {}
        self._transaction_owner: int | None = None
        self._transaction_failed = False
        # Serialize code-ingest planning as well as publishing. Parsing stays
        # outside the write transaction, so ordinary writes can still proceed.
        self.code_ingest_lock = threading.RLock()
        self._readers: queue.Queue = queue.Queue()
        self._readers_created = 0
        self._readers_lock = threading.Lock()
        # LadybugDB >= 0.20.2 exposes a plan-cache kill switch; older runtimes
        # need the private-cache eviction workaround (see _clear_prepared_cache).
        self.plan_cache_disabled = self._disable_plan_cache(self._write_conn)
        self._set_timeout(self._write_conn)
        try:
            self._preload_extensions()
            if not read_only:
                if self._retire_native_vector_indexes():
                    # Retiring the extension index changes internal catalog
                    # tables. Finish that migration and start a fresh native
                    # session before any user writes can enter a new WAL.
                    self._run(self._write_conn, "CHECKPOINT", None)
                    self.close()
                    self._db = self._open_db(db_path, config)
                    self._write_conn = lb.Connection(self._db)
                    self.plan_cache_disabled = self._disable_plan_cache(self._write_conn)
                    self._set_timeout(self._write_conn)
                    self._preload_extensions()
                self._stamp_version()
        except BaseException:
            self.close()
            raise

    def _open_db(self, db_path: str, config: GragConfig) -> lb.Database:
        """Strict replay on normal opens; lossy replay is confined to recovery copies."""
        from grag.recovery import is_replay_error

        kwargs: dict[str, Any] = {"buffer_pool_size": config.buffer_pool_size}
        if self.read_only:
            kwargs["read_only"] = True
        if self.wal_recovered:
            kwargs["throw_on_wal_replay_failure"] = False
        try:
            try:
                return lb.Database(db_path, **kwargs)
            except TypeError:
                kwargs.pop("buffer_pool_size")
                return lb.Database(db_path, **kwargs)
        except Exception as exc:
            if db_path == ":memory:" or not is_replay_error(str(exc)):
                raise
            legacy = (
                " GRAG_WAL_AUTO_RECOVER no longer enables destructive in-place recovery."
                if config.wal_auto_recover else ""
            )
            raise ConfigurationError(
                f"Database replay failed: {exc}",
                hint="Stop processes using this database, then run grag --db <file> recover "
                "to preserve its files and recover a separate copy. Do not delete the WAL "
                "or shadow file: committed writes may depend on them. Reindex only works "
                "after a database can open." + legacy,
            ) from exc

    def _retire_native_vector_indexes(self) -> bool:
        """Remove grag's derived HNSW indexes before exposing a writable engine.

        LadybugDB 0.20.2 can segfault when refilling invalidated embeddings in an
        existing HNSW index, even with one writer. Exact cosine retrieval uses
        the same stored vectors without native index maintenance. Do this on
        every writable open, including with embeddings disabled and on recovery
        copies; read-only inspection leaves the file untouched.
        """
        try:
            indexes = self._run(self._write_conn, "CALL SHOW_INDEXES() RETURN *", None).as_dicts()
        except GragError as exc:
            raise ConfigurationError(
                f"Cannot inspect native vector indexes before opening for writes: {exc}",
                hint="Keep the database and sidecars; verify the supported LadybugDB runtime.",
            ) from exc
        native = [idx for idx in indexes if str(idx["index_type"]).upper() == "HNSW"]
        # Validate the entire set before retiring anything. Never silently drop
        # externally managed indexes or permit their unsafe maintenance on writes.
        for idx in native:
            table, name = str(idx["table_name"]), str(idx["index_name"])
            if name != f"grag_vec__{table}" or idx["property_names"] != [EMBEDDING_PROP]:
                raise ConfigurationError(
                    f"Native vector index '{name}' on '{table}' is not managed by grag.",
                    hint="Native HNSW updates can crash this runtime. Preserve a backup and "
                    "have its owner remove this derived index before opening for writes; "
                    "read-only inspection remains available. Stored vectors need not be removed.",
                )
            validate_identifier(table)
            if not idx["extension_loaded"]:
                raise ConfigurationError(
                    f"Cannot retire native vector index '{name}': VECTOR extension is unavailable.",
                    hint="Restore the VECTOR extension matching the installed LadybugDB runtime "
                    "and retry. Keep the database and sidecars; do not delete stored data.",
                )
        for idx in native:
            table, name = str(idx["table_name"]), str(idx["index_name"])
            try:
                self._run(
                    self._write_conn,
                    f"CALL DROP_VECTOR_INDEX('{table}', '{name}')",
                    None,
                )
            except GragError as exc:
                raise ConfigurationError(
                    f"Cannot retire unsafe native vector index '{name}': {exc}",
                    hint="Keep the database and sidecars; resolve the index-removal error "
                    "before serving this database for writes.",
                ) from exc
            logger.warning(
                "Retired native vector index %s on %s; embeddings are preserved and "
                "semantic search uses exact cosine scoring.", name, table,
            )
        return bool(native)

    def _preload_extensions(self) -> None:
        """LOAD FTS and VECTOR once at startup so no operation path has to.

        Any table that has an FTS or HNSW index rejects reads, writes, and
        index maintenance while its extension is unloaded in the process
        ("Trying to insert into an index ... but its extension is not loaded").
        Loading per-path (search/write/vector) leaves gaps that only surface
        when a fresh process takes a different first path. Loading here covers
        every path uniformly.

        Tolerant by design: an extension that was never INSTALLed (fully
        offline) cannot have built an index, so a failed LOAD is safe to skip.
        Extensions are scoped to the Database, so the write connection's LOAD
        covers the pooled read connections too.
        """
        for name in ("FTS", "VECTOR"):
            with suppress(GragError):
                self._run(self._write_conn, f"LOAD EXTENSION {name}", None)

    # -- version stamp ---------------------------------------------------------

    _META_KV_TABLE = "_grag_meta"

    def _stamp_version(self) -> None:
        """Record which grag versions have touched this database.

        ``_grag_meta`` holds ``created_version`` (grag version at first stamp;
        "unknown" for databases that predate stamping) and ``newest_version``
        (highest grag version that has opened the file). When the database was
        last written by a *newer* grag than the one running, warn: an older
        runtime may misread structures a newer version introduced. Best-effort
        throughout — a stamp failure must never block opening the database.
        """
        from grag import __version__

        try:
            res = self._run(self._write_conn, "CALL SHOW_TABLES() RETURN *", None)
            tables = {str(row[1]) for row in res.rows}
            if self._META_KV_TABLE not in tables:
                self._run(
                    self._write_conn,
                    f"CREATE NODE TABLE {self._META_KV_TABLE}"
                    "(key STRING PRIMARY KEY, value STRING)",
                    None,
                )
                created = __version__ if len(tables) == 0 else "unknown"
                self._set_meta("created_version", created)
            newest = self._get_meta("newest_version")
            if newest is not None and _version_tuple(newest) > _version_tuple(
                __version__
            ):
                logger.warning(
                    "Database %s was last written by grag %s; you are running "
                    "grag %s. Upgrade gragdb (pip install -U gragdb) to avoid "
                    "compatibility issues.",
                    self.config.db_path,
                    newest,
                    __version__,
                )
            elif newest is None or _version_tuple(newest) < _version_tuple(
                __version__
            ):
                self._set_meta("newest_version", __version__)
        except GragError:
            logger.debug("version stamp skipped for %s", self.config.db_path)

    def _get_meta(self, key: str) -> str | None:
        res = self._run(
            self._write_conn,
            f"MATCH (m:{self._META_KV_TABLE} {{key: $k}}) RETURN m.value",
            {"k": key},
        )
        return str(res.rows[0][0]) if res.rows and res.rows[0][0] is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        # MERGE: evict the cached plan first (see execute_write) or a second
        # _set_meta for the same table takes the CREATE branch and raises
        # duplicate-PK. This runs on the write conn directly (init-time, may be
        # inside the write lock) rather than via execute_write.
        cypher = (
            f"MERGE (m:{self._META_KV_TABLE} {{key: $k}}) "
            "ON CREATE SET m.value = $v ON MATCH SET m.value = $v"
        )
        self._clear_prepared_write_cache()
        self._run(self._write_conn, cypher, {"k": key, "v": value})

    # -- execution -------------------------------------------------------------

    def execute(
        self, cypher: str, params: dict[str, Any] | None = None, *, max_rows: int | None = None
    ) -> EngineResult:
        """Run a read query on a pooled connection.

        LadybugDB/ladybug#877 showed re-executed parameterized reads (joins,
        ORDER BY, LIMIT/top-k — which covers QUERY_FTS_INDEX and
        QUERY_VECTOR_INDEX, grag's seed shapes) replaying the first execution's
        rows: the FTS seed query text is identical for every search on a table,
        so a warm reader pool would answer every search with the first one's
        results. The engine disables the plan cache on every connection
        (0.20.2+) and, on older runtimes, evicts the prepared statements before
        each read. See tests/test_read_staleness.py.
        """
        if self._transaction_owner == threading.get_ident():
            # Introspection and endpoint checks in a mutation must see the
            # transaction's own writes on the same connection.
            return self.execute_write(cypher, params)
        conn = self._borrow_reader()
        try:
            self._clear_prepared_cache(conn)
            return self._run(conn, cypher, params, max_rows=max_rows)
        finally:
            self._readers.put(conn)

    def execute_write(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> EngineResult:
        """Run a write or DDL statement, serialized on the write connection.

        Ladybug keys its implicit prepared-statement cache on query text and
        parameter type signature. On 0.20.0/0.20.1, repeated writes with
        same-typed params reused stale first-execution state instead of
        re-scanning with the new parameters — silent corruption across the
        write path: a second upsert of the same node raised duplicate-PK
        instead of updating; a second ``define_schema`` failed the same way;
        every edge after the first of its type collapsed onto the first edge's
        endpoints; a parameterized DELETE (edge pruning) targeted the first
        row's endpoints. The plan cache is therefore disabled on every
        connection (0.20.2+ kill switch), with per-statement eviction as the
        fallback on older runtimes. See
        tests/test_mutate.py::test_upsert_edges_distinct_endpoints_*.
        """
        with self._write_lock:
            self.schema_cache.clear()
            if cypher.lstrip().upper().startswith(("ALTER ", "CREATE NODE TABLE ", "CREATE REL TABLE ", "DROP TABLE ", "COMMIT", "ROLLBACK")):
                # The Python prepared statement can retain a bound RETURN n
                # shape after ALTER even with physical plan caching disabled.
                # Commit invalidation also covers readers used during the DDL
                # transaction; rollback must invalidate its temporary shape.
                self._catalog_generation += 1
            if self._transaction_failed:
                raise CypherError(
                    "The write transaction has failed; further statements are refused.",
                    hint="Exit the transaction and retry the complete operation.",
                )
            try:
                self._clear_prepared_cache(self._write_conn)
                return self._run(self._write_conn, cypher, params)
            except BaseException:
                if self._transaction_owner == threading.get_ident():
                    # Some engine errors abort the native transaction. Never
                    # let a caught error turn later statements into autocommits.
                    self._transaction_failed = True
                raise
            finally:
                # Ladybug creates the WAL lazily on the first write.
                self._secure_database_files()

    @contextmanager
    def serialized_writes(self) -> Iterator[None]:
        """Exclude competing writers across a short read/check/write sequence.

        Reentrant, including inside write_transaction. This is a lock, not
        a transaction: statements still autocommit unless one is active.
        Use execute_write for reads that need the writer's catalog.
        """
        with self._write_lock:
            yield

    @property
    def in_write_transaction(self) -> bool:
        return self._transaction_owner == threading.get_ident()

    @contextmanager
    def atomic_writes(self) -> Iterator[None]:
        """Join an enclosing ingest transaction or own this mutation's commit.

        Joining does not imply a savepoint. Even a caught Python validation
        failure poisons the outer transaction so partial writes cannot commit.
        """
        with self._write_lock:
            if self.in_write_transaction:
                try:
                    yield
                except BaseException:
                    self._transaction_failed = True
                    raise
            else:
                with self.write_transaction():
                    yield

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        """Commit one DML operation atomically on the serialized writer.

        Reads on this thread share the writer and see their own changes;
        other threads keep reading committed state through the reader pool.
        Prepare schema and expensive input parsing before entering. Nested
        transactions are rejected rather than implying savepoint semantics.
        """
        with self._write_lock:
            if self._transaction_owner is not None:
                raise CypherError(
                    "Nested write transactions are not supported.",
                    hint="Use the existing transaction for the complete operation.",
                )
            self.execute_write("BEGIN TRANSACTION")
            self._transaction_owner = threading.get_ident()
            try:
                yield
                self.execute_write("COMMIT")
            except BaseException:
                # A native statement failure may already have rolled back;
                # preserve the original error if ROLLBACK says so. Bypass
                # execute_write's failed-transaction guard for cleanup only.
                with suppress(GragError):
                    self._clear_prepared_cache(self._write_conn)
                    self._run(self._write_conn, "ROLLBACK", None)
                raise
            finally:
                self._transaction_owner = None
                self._transaction_failed = False
                self._catalog_generation += 1
                self._secure_database_files()

    # Ladybug's Python layer memoises PreparedStatement objects per (query
    # text, param types) without bound. With the plan cache disabled that memo
    # is harmless for correctness but grows with every distinct user Cypher
    # text on a long-running server, so it is trimmed past this size.
    _PREPARED_MEMO_LIMIT = 256

    def _disable_plan_cache(self, conn: lb.Connection) -> bool:
        """Turn off LadybugDB's cached-physical-plan fast path on `conn`.

        ``enable_cached_prepared_statement`` (0.20.2+) is the upstream kill
        switch for the class of bugs behind LadybugDB/ladybug#841/#870/#877:
        a re-executed parameterized statement replaying its first execution's
        state. With it set to ``'none'`` every execution maps a fresh plan and
        no private-internals eviction is needed. Returns False on runtimes
        that do not know the setting (the fallback path handles them).
        """
        try:
            self._run(conn, "CALL enable_cached_prepared_statement='none'", None)
        except GragError:
            return False
        return True

    def _clear_prepared_write_cache(self) -> None:
        """Drop every cached prepared statement on the write connection."""
        self._clear_prepared_cache(self._write_conn)

    def _clear_prepared_cache(self, conn: lb.Connection) -> None:
        """Keep a connection's prepared-statement memo from replaying stale plans.

        With the plan cache disabled at the engine level (0.20.2+), cached
        entries are safe to reuse — only their unbounded growth is a concern,
        so the memo is trimmed best-effort once it passes _PREPARED_MEMO_LIMIT.

        Fallback (older runtimes): drop every cached prepared statement before
        each execution. This reaches into ladybug's private cache; a runtime
        without those internals cannot apply the workaround, so the query is
        refused rather than risking silent corruption. Clearing only the
        caller's query text is insufficient: Ladybug rewrites some
        parameterized statements before caching them (notably ``to_json`` and
        BLOB parameters), so the cache key can differ from the input Cypher.
        """
        cache = getattr(conn, "_pybind_implicit_prepared_cache", None)
        lock = getattr(conn, "_prepared_cache_lock", None)
        catalog_current = self._prepared_catalog.get(conn) == self._catalog_generation
        if self.plan_cache_disabled and catalog_current:
            if cache is not None and lock is not None:
                with suppress(Exception):
                    if len(cache) > self._PREPARED_MEMO_LIMIT:
                        self._evict_prepared(cache, lock)
            return
        if cache is None or lock is None:
            raise ConfigurationError(
                "LadybugDB query safety check failed: the runtime does not "
                "expose the prepared-statement cache internals grag requires; "
                "refusing the query to prevent cached-plan data corruption.",
                hint="Install the verified runtime with: pip install 'ladybug==0.20.2'.",
            )
        self._evict_prepared(cache, lock)
        self._prepared_catalog[conn] = self._catalog_generation

    @staticmethod
    def _evict_prepared(cache: dict, lock: Any) -> None:
        with lock:
            prepared_statements = list(cache.values())
            cache.clear()
        # Do not invoke driver cleanup while holding its private cache lock;
        # a future close() implementation may acquire connection state itself.
        for prepared in prepared_statements:
            close = getattr(prepared, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()

    def _secure_database_files(self) -> None:
        """Keep the database and its transient WAL private to the owner."""

        if self._db_path is None or os.name == "nt":
            return
        for path in (self._db_path, Path(f"{self._db_path}.wal")):
            with suppress(FileNotFoundError):
                path.chmod(0o600)

    def _run(
        self, conn: lb.Connection, cypher: str, params: dict[str, Any] | None, *, max_rows: int | None = None
    ) -> EngineResult:
        charge("statements")
        try:
            results = conn.execute(cypher, params or {})
            # The bindings return a list of QueryResults for multi-statement
            # strings; grag only ever sends one statement at a time.
            result = results[-1] if isinstance(results, list) else results
            columns = list(result.get_column_names())
            rows: list[list[Any]] = []
            try:
                while (max_rows is None or len(rows) < max_rows) and result.has_next():
                    charge("result_rows")
                    row = [_plain(v) for v in result.get_next()]
                    charge_result(row)
                    rows.append(row)
            finally:
                result.close()
            return EngineResult(columns, rows)
        except GragError:
            raise
        except Exception as exc:
            if str(exc).startswith("Buffer manager exception:") and "buffer pool is full" in str(exc):
                from grag.core.errors import ResourceLimitError

                raise ResourceLimitError("native_buffer_bytes", self.config.buffer_pool_size,
                    hint="Narrow the query/labels/hops or restart with more GRAG_BUFFER_POOL_MB if the machine has capacity. The default is 256 MiB; this is a memory limit, not a Cypher syntax error.") from exc
            raise CypherError(
                str(exc),
                hint="Check Cypher syntax and confirm table/property names via describe_schema.",
            ) from exc

    # -- extensions --------------------------------------------------------------

    def load_extension(self, name: str) -> None:
        """INSTALL (best effort) + LOAD an extension such as FTS or VECTOR."""
        with suppress(CypherError):
            # already installed, or a build with bundled extensions
            self.execute_write(f"INSTALL {name}")
        try:
            self.execute_write(f"LOAD EXTENSION {name}")
        except CypherError as exc:
            raise GragError(
                f"Extension '{name}' is unavailable: {exc.message}",
                hint="First INSTALL requires network access; subsequent runs load from disk.",
            ) from exc

    # -- internals -----------------------------------------------------------------

    def _borrow_reader(self) -> lb.Connection:
        try:
            return self._readers.get_nowait()
        except queue.Empty:
            with self._readers_lock:
                if self._readers_created < self.config.max_read_conns:
                    self._readers_created += 1
                    conn = lb.Connection(self._db)
                    if self.plan_cache_disabled:
                        self._disable_plan_cache(conn)
                    self._set_timeout(conn)
                    return conn
            return self._readers.get()  # all busy: wait for one to come back

    def _set_timeout(self, conn: lb.Connection) -> None:
        setter = getattr(conn, "set_query_timeout", None)
        if callable(setter) and self.config.statement_timeout_ms:
            with suppress(Exception):  # best-effort driver knob
                setter(self.config.statement_timeout_ms)

    def close(self) -> None:
        # Flush the WAL to the main database file so that if the process is
        # restarted immediately there is no WAL to replay (and no replay failure
        # risk). Suppress failures: the write connection may already be closed or
        # the DB may be read-only.
        if not self.read_only:
            with suppress(Exception):
                self._run(self._write_conn, "CHECKPOINT", None)
        conns = [self._write_conn]
        while True:
            try:
                conns.append(self._readers.get_nowait())
            except queue.Empty:
                break
        for conn in conns:
            close = getattr(conn, "close", None)
            if callable(close):
                with suppress(Exception):  # close() must never raise
                    close()
        # The Database holds the buffer manager's (very large) virtual mapping;
        # without closing it, processes that open many engines leak address space.
        db_close = getattr(self._db, "close", None)
        if callable(db_close):
            with suppress(Exception):  # close() must never raise
                db_close()

    def __enter__(self) -> Engine:  # noqa: PYI034 — Self needs typing_extensions on py3.10
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _version_tuple(version: str) -> tuple[int, ...]:
    """Lenient "0.3.7" -> (0, 3, 7); unparseable parts end the tuple."""
    parts: list[int] = []
    for piece in version.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


# --- value normalization -----------------------------------------------------------


def _plain(v: Any) -> Any:
    """Convert ladybug values to plain python (recursing into dicts/lists)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def is_internal_label(name: Any) -> bool:
    """grag-internal tables (e.g. the _grag_tables registry) use the reserved
    "_" prefix; they are never part of the user-facing data model."""
    return isinstance(name, str) and name.startswith(RESERVED_PREFIX)


def is_node_value(v: Any) -> bool:
    return isinstance(v, dict) and "_ID" in v and "_LABEL" in v and "_SRC" not in v


def is_rel_value(v: Any) -> bool:
    return isinstance(v, dict) and "_SRC" in v and "_DST" in v


def is_path_value(v: Any) -> bool:
    return isinstance(v, dict) and "_NODES" in v and "_RELS" in v


def _internal_key(id_dict: Any) -> tuple:
    if isinstance(id_dict, dict):
        return (id_dict.get("table"), id_dict.get("offset"))
    return (None, None)


def node_record_from_value(
    v: dict, pk_by_label: dict[str, str] | None = None
) -> NodeRecord:
    label = str(v.get("_LABEL", ""))
    pk = (pk_by_label or {}).get(label)
    if pk and pk in v:
        nid = make_node_id(label, v[pk])
    else:
        table, offset = _internal_key(v.get("_ID"))
        nid = f"{label}#{table}:{offset}"
    # None values appear when a MATCH spans labels (LadybugDB returns the union
    # schema); omit them — null props are noise for LLM/UI consumers.
    props = {
        k: val
        for k, val in v.items()
        if k not in _HIDDEN_NODE_PROPS and val is not None
    }
    return NodeRecord(id=nid, label=label, properties=props)


def edge_record_from_value(v: dict, id_of_internal: dict[tuple, str]) -> EdgeRecord:
    rtype = str(v.get("_LABEL") or v.get("_TYPE") or "REL")
    src = id_of_internal.get(_internal_key(v.get("_SRC")), _fallback_ref(v.get("_SRC")))
    dst = id_of_internal.get(_internal_key(v.get("_DST")), _fallback_ref(v.get("_DST")))
    props = {
        k: val for k, val in v.items() if k not in _HIDDEN_REL_PROPS and val is not None
    }
    return EdgeRecord(
        id=f"{rtype}:{src}->{dst}", type=rtype, source=src, target=dst, properties=props
    )


def _fallback_ref(ref: Any) -> str:
    table, offset = _internal_key(ref)
    return f"#{table}:{offset}"


def _has_internal_graph_value(cell: Any) -> bool:
    """True if a result cell holds (or nests) a node/rel from an internal table."""
    if is_node_value(cell) or is_rel_value(cell):
        label = cell.get("_LABEL") or cell.get("_TYPE")
        return is_internal_label(label)
    if is_path_value(cell):
        return any(
            _has_internal_graph_value(x)
            for x in (*cell.get("_NODES", []), *cell.get("_RELS", []))
        )
    if isinstance(cell, (list, tuple)):
        return any(_has_internal_graph_value(x) for x in cell)
    if isinstance(cell, dict):
        return any(_has_internal_graph_value(x) for x in cell.values())
    return False


def drop_internal_rows(result: EngineResult) -> EngineResult:
    """Remove rows that surface nodes/rels from internal tables (_-prefixed).

    Applied to user-facing read paths (cypher_query): a bare MATCH (n) spans
    every node table including the _grag_tables registry, and those rows are
    noise for callers. Introspection code queries the catalog via
    engine.execute directly, so it is unaffected.
    """
    rows = [
        row
        for row in result.rows
        if not any(_has_internal_graph_value(cell) for cell in row)
    ]
    return EngineResult(result.columns, rows)


def extract_subgraph(
    result: EngineResult, pk_by_label: dict[str, str] | None = None
) -> Subgraph:
    """Collect every node/rel (including inside paths/lists) in a result set.

    Two passes: nodes first so rel endpoints resolve to canonical node ids.
    Rels whose endpoints were not returned in the same result get internal-id
    references — always RETURN endpoints alongside rels in grag's own queries.
    """
    node_vals: list[dict] = []
    rel_vals: list[dict] = []

    def walk(v: Any) -> None:
        if is_node_value(v):
            node_vals.append(v)
        elif is_rel_value(v):
            rel_vals.append(v)
        elif is_path_value(v):
            for n in v.get("_NODES", []):
                walk(n)
            for r in v.get("_RELS", []):
                walk(r)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    for row in result.rows:
        for cell in row:
            walk(cell)

    nodes: dict[str, NodeRecord] = {}
    id_of_internal: dict[tuple, str] = {}
    for v in node_vals:
        rec = node_record_from_value(v, pk_by_label)
        nodes.setdefault(rec.id, rec)
        id_of_internal[_internal_key(v.get("_ID"))] = rec.id

    edges: dict[str, EdgeRecord] = {}
    for v in rel_vals:
        erec = edge_record_from_value(v, id_of_internal)
        edges.setdefault(erec.id, erec)

    return Subgraph(nodes=list(nodes.values()), edges=list(edges.values()))
