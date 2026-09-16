"""Disk-spooled topology for full SVG exports, independent of query reply limits."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import TextIO

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError, SchemaError
from grag.core.ident import validate_identifier
from grag.core.schema import build_schema_document
from grag.core.types import FreshnessReport, GraphStats, make_node_id


@contextmanager
def capture_graph_export(engine: Engine, config: GragConfig, report: FreshnessReport) -> Iterator[TextIO]:
    """Copy one committed graph; release native readers/writer lock before delivery.

    Only primary keys, labels and endpoints cross the native boundary. SVG
    rendering does not use document bodies, vectors, evidence or other properties.
    Counts come from the captured records, including isolated nodes and parallel
    edges. Edge IDs are unique within this export, not persistent mutation IDs.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as out:
        def write(value: object) -> None:
            json.dump(value, out, ensure_ascii=False, separators=(",", ":"), default=str)

        with engine.serialized_writes():
            if engine.in_write_transaction:
                raise GragError("Cannot export uncommitted transaction state.")
            schema = build_schema_document(engine, config, detail="compact")
            pks: dict[str, str] = {}
            for table in schema.node_tables:
                name = validate_identifier(table.name, "node label")
                pk = next((p.name for p in table.properties if p.is_primary_key), None)
                if pk is None:
                    raise SchemaError(f"No primary key on {name}; cannot export complete graph topology.")
                pks[name] = validate_identifier(pk, "primary key")

            stats = GraphStats()
            out.write('{"subgraph":{"nodes":[')
            for name, pk in pks.items():
                stats.labels[name] = 0
                with closing(engine.iter_rows(f"MATCH (n:{name}) RETURN n.{pk} AS key")) as rows:
                    for row in rows:
                        if stats.node_count:
                            out.write(",")
                        write({"id": make_node_id(name, row["key"]), "label": name, "properties": {pk: row["key"]}})
                        stats.node_count += 1
                        stats.labels[name] += 1

            out.write('],"edges":[')
            for rel in schema.rel_tables:
                name = validate_identifier(rel.name, "relationship type")
                connections = engine.execute(f"CALL SHOW_CONNECTION('{name}') RETURN *").rows
                if len(connections) != 1:
                    raise SchemaError(f"Cannot export {name}: exactly one endpoint pair is required.")
                start, end = connections[0][:2]
                if start not in pks or end not in pks:
                    raise SchemaError(f"Cannot export {name}: endpoints are outside the user graph.")
                stats.labels[name] = 0
                # Read every relationship row; do not deduplicate parallel edges.
                query = (f"MATCH (a:{start})-[r:{name}]->(b:{end}) "
                         f"RETURN a.{pks[start]} AS source, b.{pks[end]} AS target")
                with closing(engine.iter_rows(query)) as rows:
                    for row in rows:
                        if stats.edge_count:
                            out.write(",")
                        write({"id": f"export:{stats.edge_count}", "type": name,
                               "source": make_node_id(start, row["source"]),
                               "target": make_node_id(end, row["target"]), "properties": {}})
                        stats.edge_count += 1
                        stats.labels[name] += 1
            out.write(']},"stats":')
            write(stats.model_dump(mode="json"))
            out.write(',"freshness":')
            write(report.model_dump(mode="json"))
            out.write("}")
        out.seek(0)
        yield out
