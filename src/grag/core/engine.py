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

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ladybug as lb

from grag.config import GragConfig
from grag.core.errors import CypherError, GragError
from grag.core.types import (
    EMB_CODE_PROP,
    EMBEDDING_PROP,
    EdgeRecord,
    NodeRecord,
    Subgraph,
    make_node_id,
)

# Never exposed inside NodeRecord.properties: internal identifiers and bulky
# vector payloads (retrieval reads vectors via explicit Cypher projections).
_HIDDEN_NODE_PROPS = {"_ID", "_LABEL", EMBEDDING_PROP, EMB_CODE_PROP}
_HIDDEN_REL_PROPS = {"_ID", "_LABEL", "_SRC", "_DST"}


@dataclass
class EngineResult:
    columns: list[str]
    rows: list[list[Any]]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


class Engine:
    def __init__(self, config: GragConfig):
        self.config = config
        db_path = str(config.db_path)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = lb.Database(db_path, buffer_pool_size=config.buffer_pool_size)
        except TypeError:
            # Older/newer bindings without the buffer_pool_size kwarg.
            self._db = lb.Database(db_path)
        self._write_conn = lb.Connection(self._db)
        self._write_lock = threading.Lock()
        self._readers: queue.Queue = queue.Queue()
        self._readers_created = 0
        self._readers_lock = threading.Lock()
        self._set_timeout(self._write_conn)

    # -- execution -------------------------------------------------------------

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> EngineResult:
        """Run a read query on a pooled connection."""
        conn = self._borrow_reader()
        try:
            return self._run(conn, cypher, params)
        finally:
            self._readers.put(conn)

    def execute_write(self, cypher: str, params: dict[str, Any] | None = None) -> EngineResult:
        """Run a write or DDL statement, serialized on the write connection."""
        with self._write_lock:
            return self._run(self._write_conn, cypher, params)

    def _run(
        self, conn: "lb.Connection", cypher: str, params: dict[str, Any] | None
    ) -> EngineResult:
        try:
            result = conn.execute(cypher, params or {})
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
        try:
            self.execute_write(f"INSTALL {name}")
        except CypherError:
            pass  # already installed, or a build with bundled extensions
        try:
            self.execute_write(f"LOAD EXTENSION {name}")
        except CypherError as exc:
            raise GragError(
                f"Extension '{name}' is unavailable: {exc.message}",
                hint="First INSTALL requires network access; subsequent runs load from disk.",
            ) from exc

    # -- internals -----------------------------------------------------------------

    def _borrow_reader(self) -> "lb.Connection":
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

    def _set_timeout(self, conn: "lb.Connection") -> None:
        setter = getattr(conn, "set_query_timeout", None)
        if callable(setter) and self.config.statement_timeout_ms:
            try:
                setter(self.config.statement_timeout_ms)
            except Exception:
                pass

    def close(self) -> None:
        conns = [self._write_conn]
        while True:
            try:
                conns.append(self._readers.get_nowait())
            except queue.Empty:
                break
        for conn in conns:
            close = getattr(conn, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        # The Database holds the buffer manager's (very large) virtual mapping;
        # without closing it, processes that open many engines leak address space.
        db_close = getattr(self._db, "close", None)
        if callable(db_close):
            try:
                db_close()
            except Exception:
                pass

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- value normalization -----------------------------------------------------------


def _plain(v: Any) -> Any:
    """Convert ladybug values to plain python (recursing into dicts/lists)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


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
        k: val
        for k, val in v.items()
        if k not in _HIDDEN_REL_PROPS and val is not None
    }
    return EdgeRecord(
        id=f"{rtype}:{src}->{dst}", type=rtype, source=src, target=dst, properties=props
    )


def _fallback_ref(ref: Any) -> str:
    table, offset = _internal_key(ref)
    return f"#{table}:{offset}"


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
        rec = edge_record_from_value(v, id_of_internal)
        edges.setdefault(rec.id, rec)

    return Subgraph(nodes=list(nodes.values()), edges=list(edges.values()))
