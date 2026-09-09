"""Consistent, checksummed JSONL snapshots and transactional logical restore.

Snapshot capture is serialized with writes and spooled before delivery. Format 2
preserves graph ownership, lifecycle, evidence history and retry receipts. Vectors,
indexes and runtime version stamps are rebuilt. Import restores a fresh target;
replaying the same archive is a no-op, preserving subsequent edits.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from typing import Any, TextIO

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.ident import validate_identifier
from grag.core.mutate import _table_index
from grag.core.types import META_TABLE, VECTOR_PROPS

FORMAT_VERSION = 2
MAX_RECORD_BYTES = 16 * 1024 * 1024
_STATE_TABLES = {META_TABLE, "_grag_evidence_history", "_grag_operations"}
_STATE_SCHEMAS = {
    META_TABLE: (
        "name",
        {
            "name": "STRING",
            "kind": "STRING",
            "pk": "STRING",
            "searchable": "BOOL",
            "from_label": "STRING",
            "to_label": "STRING",
        },
    ),
    "_grag_evidence_history": (
        "id",
        {
            "id": "STRING",
            "node_id": "STRING",
            "sequence": "INT64",
            "snapshot": "STRING",
            "metadata": "STRING",
        },
    ),
    "_grag_operations": (
        "id",
        {"id": "STRING", "digest": "STRING", "result": "STRING"},
    ),
}
_TYPES = {"STRING", "INT64", "DOUBLE", "BOOL", "DATE", "TIMESTAMP"}
_RESTORE_KEY = "restored_archive_sha256"
_MODULUS = 1 << 256


def _error(message: str) -> GragError:
    return GragError(
        message,
        hint="Use a complete grag export and restore into a new --db path. The input backup is not modified.",
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise _error(f"Unsupported export value type: {type(value).__name__}")


def _dump(obj: dict) -> str:
    try:
        value = json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            default=_json_default,
        )
    except (ValueError, TypeError) as exc:
        raise _error(f"Cannot encode snapshot record: {exc}") from exc
    if len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        raise _error(f"Snapshot record exceeds {MAX_RECORD_BYTES} UTF-8 bytes")
    return value


def _schema(engine: Engine) -> dict:
    nodes, rels = [], []
    for name, kind in sorted(_table_index(engine).items()):
        if name == "_grag_meta":
            continue  # stamps belong to the destination runtime
        validate_identifier(name)
        if name.startswith("_") and name not in _STATE_TABLES:
            raise _error(f"Cannot portably export unknown internal table {name}")
        columns = engine.execute(f"CALL TABLE_INFO('{name}') RETURN *").rows
        props = []
        pk = None
        for row in columns:
            prop, ctype = str(row[1]), str(row[2])
            if prop in VECTOR_PROPS:
                continue
            validate_identifier(prop)
            if ctype not in _TYPES:
                raise _error(
                    f"Cannot portably export {name}.{prop}: unsupported type {ctype}"
                )
            default = str(row[3]).upper()
            if default not in {"NULL", f"CAST(NULL AS {ctype})"}:
                raise _error(f"Cannot portably export custom default on {name}.{prop}")
            props.append({"name": prop, "type": ctype})
            if kind == "NODE" and row[4]:
                pk = prop
        table = {"name": name, "properties": sorted(props, key=lambda p: p["name"])}
        if kind == "NODE":
            if pk is None:
                raise _error(f"No supported primary key on {name}")
            table["primary_key"] = pk
            nodes.append(table)
        elif kind == "REL":
            connections = engine.execute(
                f"CALL SHOW_CONNECTION('{name}') RETURN *"
            ).rows
            if len(connections) != 1:
                raise _error(
                    f"Cannot portably export {name}: exactly one endpoint pair is required"
                )
            table.update(from_label=connections[0][0], to_label=connections[0][1])
            rels.append(table)
        else:
            raise _error(f"Cannot portably export table kind {kind} on {name}")
    return {"type": "schema", "node_tables": nodes, "rel_tables": rels}


def _records(engine: Engine, schema: dict) -> Iterator[dict]:
    pks = {t["name"]: t["primary_key"] for t in schema["node_tables"]}
    for table in schema["node_tables"]:
        name, pk = table["name"], table["primary_key"]
        props = [p["name"] for p in table["properties"] if p["name"] != pk]
        projection = ", ".join(
            [f"n.{pk} AS __key", *(f"n.{p} AS __p{i}" for i, p in enumerate(props))]
        )
        with closing(engine.iter_rows(f"MATCH (n:{name}) RETURN {projection}")) as rows:
            for row in rows:
                record = {
                    "type": "node",
                    "label": name,
                    "key": row["__key"],
                    "properties": {p: row[f"__p{i}"] for i, p in enumerate(props)},
                }
                fields = record["properties"]
                if "_source" in fields:
                    record["source"] = fields.pop("_source")
                if "_created_at" in fields:
                    record["created_at"] = fields.pop("_created_at")
                yield record
    for table in schema["rel_tables"]:
        name, start, end = table["name"], table["from_label"], table["to_label"]
        props = [p["name"] for p in table["properties"]]
        projection = ", ".join(
            [
                f"a.{pks[start]} AS __from",
                f"b.{pks[end]} AS __to",
                *(f"r.{p} AS __p{i}" for i, p in enumerate(props)),
            ]
        )
        with closing(
            engine.iter_rows(
                f"MATCH (a:{start})-[r:{name}]->(b:{end}) RETURN {projection}"
            )
        ) as rows:
            for row in rows:
                record = {
                    "type": "edge",
                    "rel": name,
                    "from": row["__from"],
                    "to": row["__to"],
                    "properties": {p: row[f"__p{i}"] for i, p in enumerate(props)},
                }
                fields = record["properties"]
                if "_source" in fields:
                    record["source"] = fields.pop("_source")
                yield record


class Contents:
    """Order-independent full-record checks for strict reopen verification.

    Counts plus sums of SHA256 record digests preserve duplicate-edge multiplicity
    without sorting/materializing the graph. Archive byte integrity uses a separate
    conventional SHA256 of every preceding line.
    """

    def __init__(self, schema: dict):
        self.tables = {
            t["name"]: {"records": 0, "sha256_sum": 0}
            for t in schema["node_tables"] + schema["rel_tables"]
        }

    def add(self, record: dict) -> None:
        table = record.get("label") if record["type"] == "node" else record.get("rel")
        if table not in self.tables:
            raise _error(f"Record refers to undeclared table {table!r}")
        state = self.tables[table]
        state["records"] += 1
        state["sha256_sum"] = (
            state["sha256_sum"]
            + int(hashlib.sha256(_dump(record).encode()).hexdigest(), 16)
        ) % _MODULUS

    def manifest(self) -> dict:
        return {
            table: {
                "records": values["records"],
                "sha256_sum": f"{values['sha256_sum']:064x}",
            }
            for table, values in self.tables.items()
        }


@contextmanager
def capture_snapshot(engine: Engine) -> Iterator[TextIO]:
    """Capture under the writer lock; release it before exposing the spool."""
    import grag

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as out:
        digest = hashlib.sha256()
        lines = 0

        def write(record: dict) -> None:
            nonlocal lines
            line = _dump(record) + "\n"
            digest.update(line.encode("utf-8"))
            out.write(line)
            lines += 1

        with engine.serialized_writes():
            if engine.in_write_transaction:
                raise _error("Cannot export uncommitted transaction state")
            schema = _schema(engine)  # complete preflight before producing a header
            tables = _validate_schema(schema)
            write(
                {
                    "type": "grag_export",
                    "format_version": FORMAT_VERSION,
                    "grag_version": grag.__version__,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "history": "preserved",
                    "retry_receipts": "preserved",
                    "excluded": ["vectors", "indexes", "runtime_version_stamps"],
                    "snapshot": "serialized_writes",
                }
            )
            write(schema)
            contents = Contents(schema)
            for record in _records(engine, schema):
                _validate_registry(record, tables)
                contents.add(record)
                write(record)
            out.write(
                _dump(
                    {
                        "type": "complete",
                        "lines": lines,
                        "sha256": digest.hexdigest(),
                        "tables": contents.manifest(),
                    }
                )
                + "\n"
            )
        out.seek(0)
        yield out


def export_lines(engine: Engine) -> Iterator[str]:
    with capture_snapshot(engine) as snapshot:
        for line in snapshot:
            yield line.rstrip("\n")


def export_to(engine: Engine, out: TextIO) -> int:
    with capture_snapshot(engine) as snapshot:
        return _copy(snapshot, out)


def _copy(lines: Iterable[str], out: TextIO) -> int:
    count = 0
    for line in lines:
        out.write(line if line.endswith("\n") else line + "\n")
        count += 1
    return count


def _decode(raw: str, lineno: int) -> dict:
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES + 1:
        raise _error(f"Line {lineno} exceeds the snapshot record limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def invalid_number(value: str) -> None:
        raise ValueError(f"invalid number {value}")

    try:
        record = json.loads(
            raw, object_pairs_hook=unique, parse_constant=invalid_number
        )
    except (ValueError, RecursionError) as exc:
        raise _error(f"Line {lineno}: not valid JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise _error(f"Line {lineno}: snapshot record must be an object")
    return record


def _validate_schema(schema: dict) -> dict[str, dict]:
    if set(schema) != {"type", "node_tables", "rel_tables"}:
        raise _error("Invalid schema record")
    tables: dict[str, dict] = {}
    for kind, key in (("node", "node_tables"), ("edge", "rel_tables")):
        if not isinstance(schema[key], list):
            raise _error("Schema tables must be lists")
        for table in schema[key]:
            expected = (
                {"name", "properties", "primary_key"}
                if kind == "node"
                else {"name", "properties", "from_label", "to_label"}
            )
            if not isinstance(table, dict) or set(table) != expected:
                raise _error("Invalid table definition")
            name = validate_identifier(table["name"])
            if name in tables or (name.startswith("_") and name not in _STATE_TABLES):
                raise _error(f"Duplicate or unsupported internal table {name}")
            if not isinstance(table["properties"], list):
                raise _error(f"Invalid properties for {name}")
            columns = {}
            for prop in table["properties"]:
                if not isinstance(prop, dict) or set(prop) != {"name", "type"}:
                    raise _error(f"Invalid property on {name}")
                pn = validate_identifier(prop["name"])
                if (
                    pn in columns
                    or not isinstance(prop["type"], str)
                    or prop["type"] not in _TYPES
                    or pn in VECTOR_PROPS
                ):
                    raise _error(f"Unsupported/duplicate column {name}.{pn}")
                columns[pn] = prop["type"]
            if (
                kind == "node"
                and validate_identifier(table["primary_key"]) not in columns
            ):
                raise _error(f"Missing primary key declaration for {name}")
            if name in _STATE_SCHEMAS:
                pk, expected_columns = _STATE_SCHEMAS[name]
                if (
                    kind != "node"
                    or table["primary_key"] != pk
                    or columns != expected_columns
                ):
                    raise _error(
                        f"Unsupported internal state schema for {name}; use a compatible grag version"
                    )
            tables[name] = {**table, "kind": kind, "columns": columns}
    for table in tables.values():
        if table["kind"] == "edge":
            for side in ("from_label", "to_label"):
                if (
                    tables.get(validate_identifier(table[side]), {}).get("kind")
                    != "node"
                ):
                    raise _error(f"Unknown endpoint of {table['name']}")
    return tables


def _values(record: dict, table: dict) -> dict:
    props = record.get("properties")
    if not isinstance(props, dict):
        raise _error("Record properties must be an object")
    values = dict(props)
    for field, prop in (("source", "_source"), ("created_at", "_created_at")):
        if field in record:
            if prop in values:
                raise _error(f"Duplicate {prop} in record")
            values[prop] = record[field]
    if table["kind"] == "node":
        pk = table["primary_key"]
        if pk in values or record.get("key") is None:
            raise _error("Missing or duplicated node key")
        values[pk] = record["key"]
    if set(values) != set(table["columns"]):
        raise _error(f"Record columns do not match {table['name']}")
    for name, value in values.items():
        if value is None:
            continue
        typ = table["columns"][name]
        valid = (
            (typ == "BOOL" and type(value) is bool)
            or (
                typ == "INT64"
                and type(value) is int
                and -(1 << 63) <= value < (1 << 63)
            )
            or (
                typ == "DOUBLE"
                and type(value) in (int, float)
                and -1.7976931348623157e308 <= value <= 1.7976931348623157e308
                and math.isfinite(value)
            )
            or (typ in {"STRING", "DATE", "TIMESTAMP"} and isinstance(value, str))
        )
        if not valid:
            raise _error(f"Invalid {typ} value for {table['name']}.{name}")
    return values


def _validate_registry(record: dict, tables: dict[str, dict]) -> None:
    """Catalog overrides must describe the actual exported schema."""
    if record.get("label") != META_TABLE:
        return
    name, props = record["key"], record["properties"]
    table = tables.get(name)
    if table is None or name.startswith("_"):
        raise _error("Table registry refers to an unknown/publicly unusable table")
    expected_kind = "node" if table["kind"] == "node" else "rel"
    if props["kind"] != expected_kind or (props["pk"] or "") != table.get(
        "primary_key", ""
    ):
        raise _error(f"Table registry kind/primary key differs from {name}")
    for side in ("from_label", "to_label"):
        if (props[side] or "") != table.get(side, ""):
            raise _error(f"Table registry endpoint differs from {name}")


class Archive:
    def __init__(
        self, file: TextIO, schema: dict, footer: dict, *, legacy: bool = False
    ):
        self.file, self.schema, self.footer, self.legacy = file, schema, footer, legacy
        self.tables = _validate_schema(schema)

    def records(self) -> Iterator[dict]:
        self.file.seek(0)
        for number, raw in enumerate(self.file, 1):
            record = _decode(raw, number)
            if record["type"] in {"node", "edge"}:
                yield record

    def report(self) -> dict:
        counts = self.footer["tables"]
        return {
            "nodes": sum(
                v["records"]
                for k, v in counts.items()
                if self.tables[k]["kind"] == "node" and k not in _STATE_TABLES
            ),
            "edges": sum(
                v["records"]
                for k, v in counts.items()
                if self.tables[k]["kind"] == "edge"
            ),
            "history": counts.get("_grag_evidence_history", {}).get("records", 0),
            "retry_receipts": counts.get("_grag_operations", {}).get("records", 0),
            "verified": not self.legacy,
            "sha256": self.footer["sha256"],
            "replayed": False,
            "warnings": [
                "Legacy v1 completeness cannot be verified; history and retry receipts were not included."
            ]
            if self.legacy
            else [],
        }


@contextmanager
def read_archive(
    lines: Iterable[str], *, allow_legacy: bool = False
) -> Iterator[Archive]:
    """Validate/spool every byte before permitting a destination write."""
    lines = iter(lines)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as spool:
        digest = hashlib.sha256()
        schema = footer = None
        contents = Contents({"node_tables": [], "rel_tables": []})
        tables: dict[str, dict] = {}
        header = None
        count = 0
        for lineno, raw in enumerate(lines, 1):
            if footer is not None:
                raise _error("Unexpected data after snapshot completion marker")
            record = _decode(raw, lineno)
            kind = record.get("type")
            if not isinstance(kind, str):
                raise _error("Snapshot record type must be a string")
            line = raw if raw.endswith("\n") else raw + "\n"
            if header is None:
                if kind != "grag_export":
                    raise _error("Missing grag_export header line")
                version = record.get("format_version")
                if type(version) is not int or version not in (1, FORMAT_VERSION):
                    raise _error(
                        f"Unsupported/newer export format {version!r}; upgrade grag if needed"
                    )
                if version == 1:
                    if not allow_legacy:
                        raise _error(
                            "Legacy v1 export has no completion proof or history/receipts. Pass --allow-legacy to explicitly accept these limits"
                        )
                    from grag.transfer_legacy import upgrade

                    # The adapter validates its input and produces a strict v2
                    # stream; mark the resulting report as historically unverified.
                    with read_archive(upgrade(record, lines)) as archive:
                        archive.legacy = True
                        yield archive
                    return
                if (
                    record.get("history") != "preserved"
                    or record.get("retry_receipts") != "preserved"
                ):
                    raise _error(
                        "Unknown history/retry receipt semantics in snapshot header"
                    )
                header = record
            elif schema is None:
                if kind != "schema":
                    raise _error("Expected one schema record after header")
                schema = record
                tables = _validate_schema(schema)
                contents = Contents(schema)
            elif kind == "complete":
                if (
                    set(record) != {"type", "lines", "sha256", "tables"}
                    or record["lines"] != count
                    or record["sha256"] != digest.hexdigest()
                    or record["tables"] != contents.manifest()
                ):
                    raise _error("Snapshot completion checksum/count mismatch")
                footer = record
            elif kind in {"node", "edge"}:
                table = tables.get(
                    validate_identifier(
                        record.get("label", "")
                        if kind == "node"
                        else record.get("rel", "")
                    )
                )
                if table is None or table["kind"] != kind:
                    raise _error("Unknown table or mismatched record kind")
                allowed = (
                    {"type", "label", "key", "properties", "source", "created_at"}
                    if kind == "node"
                    else {"type", "rel", "from", "to", "properties", "source"}
                )
                if set(record) - allowed:
                    raise _error("Unknown fields in data record")
                _values(record, table)
                _validate_registry(record, tables)
                if kind == "edge" and (
                    record.get("from") is None or record.get("to") is None
                ):
                    raise _error("Missing edge endpoint key")
                contents.add(record)
            else:
                raise _error(f"Unexpected snapshot record type {kind!r}")
            spool.write(line)
            if kind != "complete":
                digest.update(line.encode("utf-8"))
                count += 1
        if header is None:
            raise _error("Missing grag_export header line")
        if schema is None or footer is None:
            raise _error("Incomplete snapshot: missing schema or completion marker")
        yield Archive(spool, schema, footer, legacy=header.get("legacy_source") is True)


def _create_schema(engine: Engine, schema: dict) -> None:
    for table in schema["node_tables"] + schema["rel_tables"]:
        columns = [f"{p['name']} {p['type']}" for p in table["properties"]]
        if "primary_key" in table:
            engine.execute_write(
                f"CREATE NODE TABLE {table['name']}({', '.join(columns)}, PRIMARY KEY({table['primary_key']}))"
            )
        else:
            engine.execute_write(
                f"CREATE REL TABLE {table['name']}(FROM {table['from_label']} TO {table['to_label']}{', ' if columns else ''}{', '.join(columns)})"
            )


def _restore_record(engine: Engine, archive: Archive, record: dict) -> None:
    table = archive.tables[
        record["label"] if record["type"] == "node" else record["rel"]
    ]
    values = _values(record, table)
    params = {f"p{i}": value for i, value in enumerate(values.values())}
    props = ", ".join(
        f"{name}: CAST($p{i} AS {table['columns'][name]})"
        for i, name in enumerate(values)
    )
    if record["type"] == "node":
        engine.execute_write(f"CREATE (n:{table['name']} {{{props}}})", params)
    else:
        start, end = (
            archive.tables[table["from_label"]],
            archive.tables[table["to_label"]],
        )
        params.update(fk=record.get("from"), tk=record.get("to"))
        result = engine.execute_write(
            f"MATCH (a:{start['name']} {{{start['primary_key']}: CAST($fk AS {start['columns'][start['primary_key']]})}}), "
            f"(b:{end['name']} {{{end['primary_key']}: CAST($tk AS {end['columns'][end['primary_key']]})}}) "
            f"CREATE (a)-[r:{table['name']} {{{props}}}]->(b) RETURN count(r)",
            params,
        )
        if result.rows != [[1]]:
            raise _error(f"Missing/ambiguous endpoint on {table['name']}")


def verify_contents(engine: Engine, archive: Archive) -> None:
    schema = _schema(engine)
    if schema != archive.schema:
        raise _error("Restored schema differs from snapshot")
    contents = Contents(schema)
    for record in _records(engine, schema):
        contents.add(record)
    if contents.manifest() != archive.footer["tables"]:
        raise _error("Restored graph/history/receipt contents differ from snapshot")


def restore_archive(engine: Engine, archive: Archive) -> dict:
    with engine.serialized_writes():
        tables = _table_index(engine)
        previous = engine.execute(
            "MATCH (m:_grag_meta {key:$key}) RETURN m.value", {"key": _RESTORE_KEY}
        ).rows
        if previous == [[archive.footer["sha256"]]]:
            return {**archive.report(), "replayed": True}
        if set(tables) - {"_grag_meta"}:
            raise _error(
                "Restore target is not empty; existing graph/history/receipts were not changed"
            )
        with engine.write_transaction():
            _create_schema(engine, archive.schema)
            for record in archive.records():
                _restore_record(engine, archive, record)
            verify_contents(engine, archive)
            engine.execute_write(
                "MERGE (m:_grag_meta {key:$key}) SET m.value=$value",
                {"key": _RESTORE_KEY, "value": archive.footer["sha256"]},
            )
    return archive.report()


def import_from(
    engine: Engine,
    config: GragConfig,
    lines: Iterable[str],
    *,
    allow_legacy: bool = False,
) -> dict:
    with read_archive(iter(lines), allow_legacy=allow_legacy) as archive:
        return restore_archive(engine, archive)


# Filesystem and HTTP publication are separate from the logical snapshot format.
def export_from_server(
    server_url: str,
    out_path: str | None,
    *,
    api_token: str | None = None,
    db_name: str | None = None,
    allow_insecure: bool = False,
) -> int:
    from grag.transfer_io import download

    return download(
        server_url,
        out_path,
        api_token=api_token,
        db_name=db_name,
        allow_insecure=allow_insecure,
    )
