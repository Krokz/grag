"""Internal ownership for document-generated edges; public writes remain authored.

Replace owned edges inside the caller's graph transaction. Unknown legacy edges
are preserved, and obsolete nodes with remaining relationships are never detached.
"""

from __future__ import annotations

from typing import Any

from grag.core.engine import Engine
from grag.core.mutate import _connection_of, _table_columns, _table_index
from grag.core.types import DOCUMENT_OWNER_PROP


def prepare_document_state(engine: Engine, labels: list[str]) -> None:
    for label in labels:
        if "_document_state" not in _table_columns(engine, label):
            engine.execute_write(f"ALTER TABLE {label} ADD _document_state STRING DEFAULT CAST(NULL AS STRING)")
            engine.execute_write(f"MATCH (n:{label}) SET n._document_state='unverified'")


def mark_current(engine: Engine, label: str, keys: list[str]) -> None:
    if keys:
        engine.execute_write(f"MATCH (n:{label}) WHERE n.id IN $keys SET n._document_state='current'", {"keys": keys})


def prepare_ownership(engine: Engine, tables: list[str]) -> None:
    """Schema preparation runs under serialized_writes, outside the transaction."""
    for table in tables:
        if DOCUMENT_OWNER_PROP not in _table_columns(engine, table):
            engine.execute_write(
                f"ALTER TABLE {table} ADD {DOCUMENT_OWNER_PROP} STRING DEFAULT CAST(NULL AS STRING)"
            )


def replace_owned_edges(
    engine: Engine,
    tables: list[str],
    owners: list[str],
    warnings: list[str],
) -> None:
    for table in tables:
        engine.execute_write(
            f"MATCH ()-[r:{table}]->() WHERE r.{DOCUMENT_OWNER_PROP} IN $owners DELETE r",
            {"owners": owners},
        )
        # Unknown ownership is deliberately different from public upserts,
        # which write an empty marker. Never infer authorship from _source.
        for owner in owners:
            rows = engine.execute(
                f"MATCH (a)-[r:{table}]->() WHERE r.{DOCUMENT_OWNER_PROP} IS NULL "
                "AND (a.id = $owner OR a.id STARTS WITH $prefix) RETURN count(r)",
                {"owner": owner, "prefix": f"{owner}#"},
            ).rows
            if rows[0][0]:
                warnings.append(
                    f"Preserved {rows[0][0]} {table} relationship(s) for {owner} "
                    "with unknown ownership from an older index. Review obsolete "
                    "links explicitly; re-ingestion cannot safely identify their author."
                )


def prune_unreferenced_nodes(
    engine: Engine,
    label: str,
    predicate: str,
    params: dict[str, Any],
    warnings: list[str],
    *,
    remove_owned_edges: bool = False,
) -> int:
    """Drop obsolete generated nodes only after preserving non-owned references."""
    candidates = engine.execute(
        f"MATCH (n:{label}) WHERE {predicate} RETURN n.id", params
    ).rows
    if not candidates:
        return 0
    keys = [row[0] for row in candidates]
    if remove_owned_edges:
        # Flat re-ingest may replace chunks previously linked to sections.
        # Remove only relationships positively owned by the document loader.
        for table, kind in _table_index(engine).items():
            if kind != "REL" or DOCUMENT_OWNER_PROP not in _table_columns(
                engine, table
            ):
                continue
            endpoints = _connection_of(engine, table) or ()
            for index, endpoint in enumerate(endpoints):
                if label != endpoint:
                    continue
                pattern = (
                    f"(n:{label})-[r:{table}]->()"
                    if index == 0
                    else f"()-[r:{table}]->(n:{label})"
                )
                engine.execute_write(
                    f"MATCH {pattern} WHERE n.id IN $keys "
                    f"AND r.{DOCUMENT_OWNER_PROP} IS NOT NULL "
                    f"AND r.{DOCUMENT_OWNER_PROP} <> '' DELETE r",
                    {"keys": keys},
                )
    rows = engine.execute_write(
        f"MATCH (n:{label}) WHERE n.id IN $keys "
        "AND NOT EXISTS { MATCH (n)-[]-() } DELETE n RETURN count(n)",
        {"keys": keys},
    ).rows
    pruned = int(rows[0][0])
    retained = len(keys) - pruned
    if retained:
        engine.execute_write(f"MATCH (n:{label}) WHERE n.id IN $keys SET n._document_state='obsolete'", {"keys": keys})
        warnings.append(
            f"Retained {retained} obsolete {label} node(s) because relationships "
            "outside this ingest still reference them. Their content may describe "
            "an earlier document revision."
        )
    return pruned
