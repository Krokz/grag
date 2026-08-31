"""LadybugDB engine wrapper — the only module in grag that imports ladybug.

Threading model: embedded LadybugDB is single-writer. Writes go through
execute_write() (serialized on one connection); reads borrow pooled
connections. All values returned to callers are plain python objects; raw
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
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ladybug as lb

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, CypherError, GragError
from grag.core.types import (
    EMB_CODE_PROP,
    EMBEDDING_PROP,
    RESERVED_PREFIX,
    EdgeRecord,
    NodeRecord,
    Subgraph,
    make_node_id,
)

logger = logging.getLogger("grag")

# Never exposed inside NodeRecord.properties: internal identifiers and bulky
# vector payloads (retrieval reads vectors via explicit Cypher projections).
_HIDDEN_NODE_PROPS = {"_ID", "_LABEL", EMBEDDING_PROP, EMB_CODE_PROP}
_HIDDEN_REL_PROPS = {"_ID", "_LABEL", "_SRC", "_DST"}


@dataclass
class EngineResult:
    columns: list[str]
    rows: list[list[Any]]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


class Engine:
    def __init__(self, config: GragConfig):
        self.config = config
        self.wal_recovered: bool = False
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
        self._secure_database_files()
        self._write_conn = lb.Connection(self._db)
        self._write_lock = threading.Lock()
        self._readers: queue.Queue = queue.Queue()
        self._readers_created = 0
        self._readers_lock = threading.Lock()
        self._set_timeout(self._write_conn)
        self._preload_extensions()
        if self.wal_recovered:
            self._drop_stale_vector_indexes()
        self._stamp_version()

    def _open_db(self, db_path: str, config: GragConfig) -> lb.Database:
        """Open the database, offering WAL auto-recovery when a TTY is attached.

        A corrupted WAL causes lb.Database() to raise. On an interactive terminal
        we explain the consequences and ask before proceeding; non-interactive
        callers (the API server, CI) get a log message and the original exception
        so the operator can run 'grag reindex' deliberately.
        """
        try:
            try:
                return lb.Database(db_path, buffer_pool_size=config.buffer_pool_size)
            except TypeError:
                return lb.Database(db_path)
        except Exception as exc:
            if db_path == ":memory:" or "wal" not in str(exc).lower():
                raise
            if not sys.stdin.isatty():
                logger.warning(
                    "WAL replay failed (%s). Run 'grag reindex' to repair the database.",
                    exc,
                )
                raise
            print(
                f"\nWARNING: WAL replay failed:\n  {exc}\n\n"
                "Auto-recovery will reopen the database in failure-tolerant mode.\n"
                "Writes since the last checkpoint are lost, and all HNSW vector\n"
                "indexes will be dropped so they can be rebuilt from clean data.\n"
                "Run 'grag reindex' afterwards to restore full search performance.\n",
                file=sys.stderr,
            )
            if input("Attempt auto-recovery? [y/N]: ").strip().lower() != "y":
                raise
            logger.warning("WAL auto-recovery approved by user for %s", db_path)
            self.wal_recovered = True
            try:
                return lb.Database(
                    db_path,
                    throw_on_wal_replay_failure=False,
                    buffer_pool_size=config.buffer_pool_size,
                )
            except TypeError:
                return lb.Database(db_path, throw_on_wal_replay_failure=False)

    def _drop_stale_vector_indexes(self) -> None:
        """After WAL recovery, drop all grag HNSW indexes (potentially stale).

        Stale HNSW entries (nodes whose embedding writes were rolled back by WAL
        recovery) cause SIGSEGV in LadybugDB's HNSW maintenance when a new
        embedding write triggers a neighbor search over NULL pointers.  Dropping
        here is safe: _ensure_vector_index() recreates the index on first search,
        building it from the embeddings that survived recovery.
        """
        try:
            res = self._run(self._write_conn, "CALL SHOW_TABLES() RETURN *", None)
        except GragError as exc:
            logger.warning(
                "WAL recovery: could not list tables to drop HNSW indexes: %s", exc
            )
            return
        for row in res.rows:
            table_name = str(row[1])
            table_type = str(row[2]).upper()
            if table_type != "NODE":
                continue
            idx = f"grag_vec__{table_name}"
            with suppress(GragError):
                self._run(
                    self._write_conn,
                    f"CALL DROP_VECTOR_INDEX('{table_name}', '{idx}')",
                    None,
                )
                logger.warning(
                    "WAL recovery: dropped stale HNSW index on %s", table_name
                )
        with suppress(GragError):
            self._run(self._write_conn, "CHECKPOINT", None)

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
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> EngineResult:
        """Run a read query on a pooled connection."""
        conn = self._borrow_reader()
        try:
            return self._run(conn, cypher, params)
        finally:
            self._readers.put(conn)

    def execute_write(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> EngineResult:
        """Run a write or DDL statement, serialized on the write connection.

        Writes always evict their cached prepared statement first so ladybug
        compiles a fresh plan every time.

        Ladybug keys its implicit prepared-statement cache on query text and
        parameter type signature. On 0.20.1, repeated writes with same-typed
        params have been observed to reuse stale first-execution state instead
        of re-scanning with the new parameters. The result is silent corruption
        across the write path: a second upsert of the same node raises
        duplicate-PK instead of updating; a second ``define_schema`` fails the
        same way; every edge after the first of its type collapses onto the
        first edge's endpoints; and a parameterized DELETE (edge pruning)
        targets the first row's endpoints instead of its own. Reads run on
        separate pooled connections and keep their cache — this eviction is
        write-only. See
        tests/test_mutate.py::test_upsert_edges_distinct_endpoints_*.
        """
        with self._write_lock:
            try:
                self._clear_prepared_write_cache()
                return self._run(self._write_conn, cypher, params)
            finally:
                # Ladybug creates the WAL lazily on the first write.
                self._secure_database_files()

    def _clear_prepared_write_cache(self) -> None:
        """Drop every cached prepared statement on the write connection.

        Reaches into ladybug's implicit prepared-statement cache. A runtime
        without these private internals cannot apply the write-safety
        workaround, so refuse the write rather than risk silent corruption.

        Clearing only the caller's query text is insufficient: Ladybug rewrites
        some parameterized statements before caching them (notably ``to_json``
        and BLOB parameters), so the cache key can differ from the input Cypher.
        The cache belongs only to the single write connection; read-connection
        caches remain untouched.
        """
        conn = self._write_conn
        cache = getattr(conn, "_pybind_implicit_prepared_cache", None)
        lock = getattr(conn, "_prepared_cache_lock", None)
        if cache is None or lock is None:
            raise ConfigurationError(
                "LadybugDB write safety check failed: the runtime does not "
                "expose the prepared-statement cache internals grag requires; "
                "refusing the write to prevent cached-plan data corruption.",
                hint="Install the verified runtime with: pip install 'ladybug==0.20.1'.",
            )
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
        self, conn: lb.Connection, cypher: str, params: dict[str, Any] | None
    ) -> EngineResult:
        try:
            results = conn.execute(cypher, params or {})
            # The bindings return a list of QueryResults for multi-statement
            # strings; grag only ever sends one statement at a time.
            result = results[-1] if isinstance(results, list) else results
            columns = list(result.get_column_names())
            rows: list[list[Any]] = []
            while result.has_next():
                rows.append([_plain(v) for v in result.get_next()])
            result.close()
            return EngineResult(columns, rows)
        except GragError:
            raise
        except Exception as exc:
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
