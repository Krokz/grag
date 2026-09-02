"""Portable JSONL export/import of a grag database.

The .lbdb file format belongs to LadybugDB and can change across storage
versions; this module gives every database a durable, human-readable escape
hatch: ``grag export`` writes one JSON object per line (header, schema, then
nodes and edges), ``grag import`` replays that stream into any grag database
via the normal schema/upsert machinery.

Contents: user tables, user properties, and provenance (``_source``; node
``_created_at`` is restored best-effort). Vector columns are deliberately
excluded — embeddings are derived data and re-embed lazily after import.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any, TextIO

from grag.config import GragConfig
from grag.core.engine import Engine, is_internal_label
from grag.core.errors import GragError
from grag.core.mutate import (
    _meta_rows,
    _node_pks,
    _rel_endpoints,
    _table_columns,
    _table_index,
    define_schema,
)
from grag.core.types import (
    PROVENANCE_CREATED_AT,
    PROVENANCE_SOURCE,
    RESERVED_PREFIX,
    VECTOR_PROPS,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
)

FORMAT_VERSION = 1


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def _user_props(columns: dict[str, str]) -> dict[str, str]:
    """Declared columns minus grag-managed ones (provenance, vectors)."""
    return {
        name: ctype
        for name, ctype in columns.items()
        if not name.startswith(RESERVED_PREFIX) and name not in VECTOR_PROPS
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_lines(engine: Engine) -> Iterator[str]:
    """Yield the JSONL export of every user table in `engine`'s database."""
    import grag

    yield _dump(
        {
            "type": "grag_export",
            "format_version": FORMAT_VERSION,
            "grag_version": grag.__version__,
        }
    )

    tables = _table_index(engine)
    meta = _meta_rows(engine, tables)
    pks = _node_pks(engine)
    endpoints = _rel_endpoints(engine)

    node_tables: list[dict] = []
    rel_tables: list[dict] = []
    node_columns: dict[str, dict[str, str]] = {}
    rel_columns: dict[str, dict[str, str]] = {}
    for name, kind in sorted(tables.items()):
        if is_internal_label(name):
            continue
        columns = _table_columns(engine, name)
        user = _user_props(columns)
        m = meta.get(name) or {}
        if kind == "NODE":
            pk = pks.get(name)
            if not pk:
                continue
            node_columns[name] = columns
            node_tables.append(
                {
                    "name": name,
                    "primary_key": pk,
                    "searchable": bool(m.get("searchable") or False),
                    "properties": [
                        {"name": n, "type": t} for n, t in user.items() if n != pk
                    ],
                }
            )
        else:
            fromto = endpoints.get(name)
            if not fromto:
                continue
            rel_columns[name] = columns
            rel_tables.append(
                {
                    "name": name,
                    "from_label": fromto[0],
                    "to_label": fromto[1],
                    "properties": [{"name": n, "type": t} for n, t in user.items()],
                }
            )

    yield _dump({"type": "schema", "node_tables": node_tables, "rel_tables": rel_tables})

    for table in node_tables:
        name, pk = table["name"], table["primary_key"]
        columns = node_columns[name]
        props = [n for n in _user_props(columns) if n != pk]
        projection = ", ".join(
            [f"n.{pk} AS __key"]
            + [f"n.{p} AS {p}" for p in props]
            + [f"n.{PROVENANCE_SOURCE} AS __source"]
            + (
                [f"n.{PROVENANCE_CREATED_AT} AS __created_at"]
                if PROVENANCE_CREATED_AT in columns
                else []
            )
        )
        res = engine.execute(f"MATCH (n:{name}) RETURN {projection}")
        for row in res.as_dicts():
            record: dict[str, Any] = {
                "type": "node",
                "label": name,
                "key": row["__key"],
                "properties": {
                    p: row.get(p) for p in props if row.get(p) is not None
                },
            }
            if row.get("__source") is not None:
                record["source"] = row["__source"]
            if row.get("__created_at") is not None:
                record["created_at"] = _json_default(row["__created_at"])
            yield _dump(record)

    for table in rel_tables:
        name = table["name"]
        from_label, to_label = table["from_label"], table["to_label"]
        from_pk, to_pk = pks.get(from_label), pks.get(to_label)
        if not from_pk or not to_pk:
            continue
        props = list(_user_props(rel_columns[name]))
        projection = ", ".join(
            [f"a.{from_pk} AS __from", f"b.{to_pk} AS __to"]
            + [f"r.{p} AS {p}" for p in props]
            + [f"r.{PROVENANCE_SOURCE} AS __source"]
        )
        res = engine.execute(
            f"MATCH (a:{from_label})-[r:{name}]->(b:{to_label}) RETURN {projection}"
        )
        for row in res.as_dicts():
            record = {
                "type": "edge",
                "rel": name,
                "from": row["__from"],
                "to": row["__to"],
                "properties": {
                    p: row.get(p) for p in props if row.get(p) is not None
                },
            }
            if row.get("__source") is not None:
                record["source"] = row["__source"]
            yield _dump(record)


def export_to(engine: Engine, out: TextIO) -> int:
    """Write the export stream to `out`; returns the number of lines."""
    count = 0
    for line in export_lines(engine):
        out.write(line + "\n")
        count += 1
    return count


def export_from_server(
    server_url: str,
    out_path: str | None,
    *,
    api_token: str | None = None,
    db_name: str | None = None,
    allow_insecure: bool = False,
) -> int:
    """Stream ``GET <server>/api/export`` (an online backup of a live server)
    into `out_path` (or stdout). Returns the number of lines written.

    Stdlib-only so a backup cron on a minimal host needs nothing beyond grag.
    """
    import sys
    import urllib.request

    from grag.proxy import validate_server_url

    origin = validate_server_url(server_url, allow_insecure=allow_insecure)
    req = urllib.request.Request(f"{origin}/api/export")  # noqa: S310 — https/loopback enforced above
    if api_token:
        req.add_header("Authorization", f"Bearer {api_token}")
    if db_name:
        req.add_header("x-grag-db", db_name)
    count = 0
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        if resp.status != 200:
            raise OSError(f"server returned HTTP {resp.status}")
        sink = open(out_path, "w", encoding="utf-8") if out_path else sys.stdout  # noqa: SIM115
        try:
            for raw in resp:
                sink.write(raw.decode("utf-8"))
                count += 1
        finally:
            if out_path:
                sink.close()
    return count


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _import_schema(engine: Engine, config: GragConfig, record: dict) -> None:
    node_tables = [
        NodeTableSpec(
            name=t["name"],
            primary_key=t["primary_key"],
            searchable=bool(t.get("searchable", False)),
            properties=[
                PropertySpec(name=p["name"], type=p.get("type", "STRING"))
                for p in t.get("properties", [])
            ],
        )
        for t in record.get("node_tables", [])
    ]
    rel_tables = [
        RelTableSpec(
            name=t["name"],
            from_label=t["from_label"],
            to_label=t["to_label"],
            properties=[
                PropertySpec(name=p["name"], type=p.get("type", "STRING"))
                for p in t.get("properties", [])
            ],
        )
        for t in record.get("rel_tables", [])
    ]
    define_schema(
        engine,
        config,
        DefineSchemaRequest(node_tables=node_tables, rel_tables=rel_tables),
    )


def _import_node(
    engine: Engine, pks: dict[str, str | None], record: dict, warnings: list[str]
) -> bool:
    label = record.get("label")
    pk = pks.get(str(label))
    if not pk:
        warnings.append(f"node skipped: unknown label {label!r}")
        return False
    columns = _table_columns(engine, str(label))
    sets: list[str] = []
    params: dict[str, Any] = {"key": record.get("key")}
    for i, (name, value) in enumerate((record.get("properties") or {}).items()):
        if name not in columns or name == pk:
            continue
        params[f"p{i}"] = value
        sets.append(f"n.{name} = ${f'p{i}'}")
    if record.get("source") is not None and PROVENANCE_SOURCE in columns:
        params["src"] = record["source"]
        sets.append(f"n.{PROVENANCE_SOURCE} = $src")
    created_at = record.get("created_at")
    has_created = PROVENANCE_CREATED_AT in columns
    cypher = f"MERGE (n:{label} {{{pk}: $key}})"
    if sets:
        cypher += " SET " + ", ".join(sets)
    try:
        if created_at and has_created:
            # Restore the original provenance timestamp when the engine can
            # parse it; fall back to now() below when it cannot.
            params["ca"] = created_at
            joined = ", ".join([*sets, f"n.{PROVENANCE_CREATED_AT} = timestamp($ca)"])
            engine.execute_write(
                f"MERGE (n:{label} {{{pk}: $key}}) SET {joined}", params
            )
        else:
            engine.execute_write(cypher, params)
        return True
    except GragError:
        if created_at and has_created:
            params.pop("ca", None)
            engine.execute_write(cypher, params)
            warnings.append(
                f"node {label}:{record.get('key')}: original _created_at not "
                "restorable; kept properties, dropped the timestamp"
            )
            return True
        raise


def _import_edge(
    engine: Engine,
    pks: dict[str, str | None],
    endpoints: dict[str, tuple[str, str]],
    record: dict,
    warnings: list[str],
) -> bool:
    rel = str(record.get("rel"))
    fromto = endpoints.get(rel)
    if not fromto:
        warnings.append(f"edge skipped: unknown rel {rel!r}")
        return False
    from_label, to_label = fromto
    from_pk, to_pk = pks.get(from_label), pks.get(to_label)
    if not from_pk or not to_pk:
        warnings.append(f"edge skipped: endpoints of {rel!r} have no primary key")
        return False
    columns = _table_columns(engine, rel)
    sets: list[str] = []
    params: dict[str, Any] = {"fk": record.get("from"), "tk": record.get("to")}
    for i, (name, value) in enumerate((record.get("properties") or {}).items()):
        if name not in columns:
            continue
        params[f"p{i}"] = value
        sets.append(f"r.{name} = ${f'p{i}'}")
    if record.get("source") is not None and PROVENANCE_SOURCE in columns:
        params["src"] = record["source"]
        sets.append(f"r.{PROVENANCE_SOURCE} = $src")
    cypher = (
        f"MATCH (a:{from_label} {{{from_pk}: $fk}}), "
        f"(b:{to_label} {{{to_pk}: $tk}}) "
        f"MERGE (a)-[r:{rel}]->(b)"
    )
    if sets:
        cypher += " SET " + ", ".join(sets)
    result = engine.execute_write(cypher + " RETURN count(r)", params)
    if not result.rows or int(result.rows[0][0]) == 0:
        warnings.append(
            f"edge {rel} {record.get('from')!r}->{record.get('to')!r}: "
            "endpoint node(s) missing; skipped"
        )
        return False
    return True


def import_from(
    engine: Engine, config: GragConfig, lines: Iterable[str]
) -> dict[str, Any]:
    """Replay a JSONL export into `engine`'s database (idempotent merge)."""
    nodes = edges = 0
    warnings: list[str] = []
    pks: dict[str, str | None] = {}
    endpoints: dict[str, tuple[str, str]] = {}
    saw_header = False
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError as exc:
            raise GragError(
                f"line {lineno}: not valid JSON ({exc}).",
                hint="grag import expects a file produced by grag export.",
            ) from None
        kind = record.get("type")
        if kind == "grag_export":
            saw_header = True
            if int(record.get("format_version", 0)) > FORMAT_VERSION:
                raise GragError(
                    f"Export format v{record.get('format_version')} is newer than "
                    f"this grag understands (v{FORMAT_VERSION}).",
                    hint="Upgrade gragdb, then re-run the import.",
                )
        elif kind == "schema":
            if not saw_header:
                raise GragError(
                    "Missing grag_export header line.",
                    hint="grag import expects a file produced by grag export.",
                )
            _import_schema(engine, config, record)
            pks = _node_pks(engine)
            endpoints = _rel_endpoints(engine)
        elif kind == "node":
            if _import_node(engine, pks, record, warnings):
                nodes += 1
        elif kind == "edge":
            if _import_edge(engine, pks, endpoints, record, warnings):
                edges += 1
        else:
            warnings.append(f"line {lineno}: unknown record type {kind!r}; skipped")
    if not saw_header:
        raise GragError(
            "Not a grag export (no header line found).",
            hint="grag import expects a file produced by grag export.",
        )
    return {"nodes": nodes, "edges": edges, "warnings": warnings}
