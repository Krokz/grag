"""Explicit opt-in adapter for incomplete-by-design v1 exports."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator

from grag.core.ident import validate_identifier
from grag.core.types import META_TABLE
from grag.transfer import Contents, _decode, _dump, _error, _validate_schema


def upgrade(header: dict, lines: Iterable[str]) -> Iterator[str]:
    digest = hashlib.sha256()
    count = 0

    def emit(record: dict) -> str:
        nonlocal count
        line = _dump(record)
        digest.update((line + "\n").encode())
        count += 1
        return line

    yield emit(
        {
            "type": "grag_export",
            "format_version": 2,
            "grag_version": header.get("grag_version", "unknown"),
            "history": "preserved",
            "retry_receipts": "preserved",
            "legacy_source": True,
        }
    )
    schema: dict | None = None
    tables: dict = {}
    contents = Contents({"node_tables": [], "rel_tables": []})
    for number, raw in enumerate(lines, 2):
        if not raw.strip():
            continue
        record = _decode(raw, number)
        if schema is None:
            if record.get("type") != "schema":
                raise _error("Expected v1 schema after header")
            schema = {"type": "schema", "node_tables": [], "rel_tables": []}
            registry = []
            for table_kind, key in (("node", "node_tables"), ("edge", "rel_tables")):
                definitions = record.get(key)
                if not isinstance(definitions, list):
                    raise _error("Legacy schema tables must be lists")
                for old in definitions:
                    required = (
                        {"name", "primary_key"}
                        if table_kind == "node"
                        else {"name", "from_label", "to_label"}
                    )
                    if not isinstance(old, dict) or not required <= set(old):
                        raise _error("Malformed legacy table definition")
                    if validate_identifier(old["name"]).startswith("_"):
                        raise _error("Unexpected internal table in legacy export")
                    old_props = old.get("properties", [])
                    if not isinstance(old_props, list) or any(
                        not isinstance(p, dict) or set(p) != {"name", "type"}
                        for p in old_props
                    ):
                        raise _error("Malformed legacy properties")
                    for prop in old_props:
                        validate_identifier(prop["name"])
                    props = list(old_props)
                    props.append({"name": "_source", "type": "STRING"})
                    table = {"name": old["name"]}
                    if table_kind == "node":
                        table["primary_key"] = validate_identifier(old["primary_key"])
                        # Old exporters omitted the primary-key type entirely.
                        props.extend(
                            [
                                {"name": old["primary_key"], "type": "STRING"},
                                {"name": "_created_at", "type": "TIMESTAMP"},
                            ]
                        )
                    else:
                        table.update(
                            from_label=old["from_label"], to_label=old["to_label"]
                        )
                    table["properties"] = sorted(props, key=lambda p: p["name"])
                    schema[key].append(table)
                    registry.append(
                        {
                            "type": "node",
                            "label": META_TABLE,
                            "key": old["name"],
                            "properties": {
                                "kind": "node" if table_kind == "node" else "rel",
                                "pk": old.get("primary_key", ""),
                                "searchable": bool(old.get("searchable", False)),
                                "from_label": old.get("from_label", ""),
                                "to_label": old.get("to_label", ""),
                            },
                        }
                    )
            schema["node_tables"].append(
                {
                    "name": META_TABLE,
                    "primary_key": "name",
                    "properties": [
                        {"name": p, "type": "BOOL" if p == "searchable" else "STRING"}
                        for p in (
                            "from_label",
                            "kind",
                            "name",
                            "pk",
                            "searchable",
                            "to_label",
                        )
                    ],
                }
            )
            for key in ("node_tables", "rel_tables"):
                schema[key].sort(key=lambda t: t["name"])
            tables = _validate_schema(schema)
            contents = Contents(schema)
            yield emit(schema)
            for row in registry:
                contents.add(row)
                yield emit(row)
            continue
        kind = record.get("type")
        if not isinstance(kind, str) or kind not in {"node", "edge"}:
            raise _error("Unknown record in legacy export")
        target = tables.get(
            validate_identifier(
                record.get("label", "") if kind == "node" else record.get("rel", "")
            )
        )
        if target is None:
            raise _error("Unknown record/table in legacy export")
        values = record.get("properties") or {}
        if not isinstance(values, dict) or any(p.startswith("_") for p in values):
            raise _error("Invalid/reserved properties in legacy export")
        fields = {
            p: values.get(p)
            for p in target["columns"]
            if p not in {target.get("primary_key"), "_source", "_created_at"}
        }
        if set(values) - set(fields):
            raise _error("Undeclared properties in legacy export")
        record["properties"] = fields
        record.setdefault("source", None)
        if kind == "node":
            if not isinstance(record.get("key"), str):
                raise _error(
                    "Legacy primary-key type was omitted; a non-string key cannot be restored safely"
                )
            record.setdefault("created_at", None)
        contents.add(record)
        yield emit(record)
    if schema is None:
        raise _error("Missing legacy schema")
    yield _dump(
        {
            "type": "complete",
            "lines": count,
            "sha256": digest.hexdigest(),
            "tables": contents.manifest(),
        }
    )
