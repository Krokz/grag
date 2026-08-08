"""Schema introspection over the LadybugDB catalog.

Reads SHOW_TABLES / TABLE_INFO / SHOW_CONNECTION plus the grag meta table
(META_TABLE) to build the SchemaDocument an LLM anchors on before writing
Cypher. Tables created via raw cypher (no meta row) are still introspected;
every query tolerates an empty database and a missing meta table.
"""

from __future__ import annotations

from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.types import (
    META_TABLE,
    GraphStats,
    NodeTableDoc,
    PropertyDoc,
    RelTableDoc,
    SchemaDocument,
)

_SAMPLE_KEY_LIMIT = 5


def _show_tables(engine: Engine) -> list[dict[str, Any]]:
    res = engine.execute("CALL SHOW_TABLES() RETURN *")
    return res.as_dicts()


def _meta_rows(engine: Engine) -> dict[str, dict[str, Any]]:
    """name -> meta row for tables recorded by define_schema; {} if absent."""
    try:
        res = engine.execute(
            f"MATCH (m:{META_TABLE}) "
            "RETURN m.name, m.kind, m.pk, m.searchable, m.from_label, m.to_label"
        )
    except GragError:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for d in res.as_dicts():
        name = d.get("m.name")
        if name:
            rows[str(name)] = {
                "kind": d.get("m.kind"),
                "pk": d.get("m.pk"),
                "searchable": bool(d.get("m.searchable") or False),
                "from_label": d.get("m.from_label") or "",
                "to_label": d.get("m.to_label") or "",
            }
    return rows


def _table_info(engine: Engine, table: str) -> list[PropertyDoc]:
    res = engine.execute(f"CALL TABLE_INFO('{table}') RETURN *")
    docs: list[PropertyDoc] = []
    for d in res.as_dicts():
        docs.append(
            PropertyDoc(
                name=str(d.get("name")),
                type=str(d.get("type")),
                is_primary_key=bool(d.get("primary key")),
            )
        )
    return docs


def pk_map(engine: Engine) -> dict[str, str]:
    """label -> primary key property name.

    META_TABLE is the primary source; TABLE_INFO's primary-key flag covers
    tables without a meta row (e.g. created via raw cypher).
    """
    mapping: dict[str, str] = {}
    try:
        tables = _show_tables(engine)
    except GragError:
        tables = []
    for t in tables:
        name = str(t.get("name"))
        if name == META_TABLE:
            continue
        try:
            for prop in _table_info(engine, name):
                if prop.is_primary_key:
                    mapping[name] = prop.name
                    break
        except GragError:
            continue
    for name, meta in _meta_rows(engine).items():
        pk = meta.get("pk")
        if pk:
            mapping[str(name)] = str(pk)  # meta wins over TABLE_INFO
    return mapping


def _row_count(engine: Engine, table: str, kind: str) -> int:
    try:
        if kind == "REL":
            res = engine.execute(f"MATCH ()-[r:{table}]->() RETURN count(r)")
        else:
            res = engine.execute(f"MATCH (n:{table}) RETURN count(n)")
        return int(res.rows[0][0]) if res.rows else 0
    except GragError:
        return 0


def _sample_keys(engine: Engine, table: str, pk: str | None) -> list[str]:
    if not pk:
        return []
    try:
        res = engine.execute(
            f"MATCH (n:{table}) RETURN n.{pk} LIMIT {_SAMPLE_KEY_LIMIT}"
        )
    except GragError:
        return []
    return [str(row[0]) for row in res.rows]


def _rel_connection(engine: Engine, table: str) -> tuple[str, str]:
    try:
        res = engine.execute(f"CALL SHOW_CONNECTION('{table}') RETURN *")
        row = res.as_dicts()[0]
        return (
            str(row.get("source table name") or ""),
            str(row.get("destination table name") or ""),
        )
    except (GragError, IndexError):
        return "", ""


def build_schema_document(engine: Engine, config: GragConfig) -> SchemaDocument:
    try:
        tables = _show_tables(engine)
    except GragError:
        tables = []
    meta = _meta_rows(engine)
    pks = pk_map(engine)

    node_docs: list[NodeTableDoc] = []
    rel_docs: list[RelTableDoc] = []
    for t in tables:
        name = str(t.get("name"))
        if name == META_TABLE:
            continue
        kind = str(t.get("type", "")).upper()
        m = meta.get(name, {})
        try:
            props = _table_info(engine, name)
        except GragError:
            props = []
        if kind == "REL":
            from_label, to_label = _rel_connection(engine, name)
            rel_docs.append(
                RelTableDoc(
                    name=name,
                    from_label=m.get("from_label") or from_label,
                    to_label=m.get("to_label") or to_label,
                    properties=props,
                    row_count=_row_count(engine, name, kind),
                )
            )
        else:
            node_docs.append(
                NodeTableDoc(
                    name=name,
                    properties=props,
                    row_count=_row_count(engine, name, kind),
                    sample_keys=_sample_keys(engine, name, pks.get(name)),
                    searchable=bool(m.get("searchable", False)),
                )
            )

    return SchemaDocument(
        node_tables=node_docs,
        rel_tables=rel_docs,
        text=_render_text(node_docs, rel_docs),
    )


def _render_text(node_docs: list[NodeTableDoc], rel_docs: list[RelTableDoc]) -> str:
    lines: list[str] = []
    for t in node_docs:
        props = ", ".join(
            f"{p.name}:{p.type} PK" if p.is_primary_key else f"{p.name}:{p.type}"
            for p in t.properties
        )
        flags = [f"{t.row_count} rows"]
        if t.searchable:
            flags.append("searchable")
        line = f"{t.name}({props}) [{', '.join(flags)}]"
        if t.sample_keys:
            quoted = ",".join(f'"{k}"' for k in t.sample_keys)
            line += f" samples: {quoted}"
        lines.append(line)
    for t in rel_docs:
        head = f"{t.from_label} -> {t.to_label}" if t.from_label or t.to_label else ""
        props = ", ".join(f"{p.name}:{p.type}" for p in t.properties)
        inner = ", ".join(part for part in (head, props) if part)
        lines.append(f"{t.name}({inner}) [{t.row_count} rows]")
    return "\n".join(lines)


def table_stats(engine: Engine) -> GraphStats:
    stats = GraphStats()
    try:
        tables = _show_tables(engine)
    except GragError:
        return stats
    for t in tables:
        name = str(t.get("name"))
        if name == META_TABLE:
            continue
        kind = str(t.get("type", "")).upper()
        count = _row_count(engine, name, kind)
        stats.labels[name] = count
        if kind == "REL":
            stats.edge_count += count
        else:
            stats.node_count += count
    return stats
