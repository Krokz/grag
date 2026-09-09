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
            engine.execute_write(
                f"ALTER TABLE {label} ADD _document_state STRING DEFAULT CAST(NULL AS STRING)"
            )
            engine.execute_write(
                f"MATCH (n:{label}) SET n._document_state='unverified'"
            )


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
    state_prop: str = "_document_state",
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
        engine.execute_write(
            f"MATCH (n:{label}) WHERE n.id IN $keys SET n.{state_prop}='obsolete'",
            {"keys": keys},
        )
        warnings.append(
            f"Retained {retained} obsolete {label} node(s) because relationships "
            "outside this ingest still reference them. Their content may describe "
            "an earlier document revision."
        )
    return pruned


class DocumentSync:
    """Reconcile one named source across labels/modes, retaining its identity.

    Loader-origin paths make a directory authoritative even for JSON batches
    whose document source is a URL. Only known document tables participate.
    """

    def __init__(self, engine: Engine, req):
        from grag.ingest.loaders import _normalized_source

        self.engine, self.req = engine, req
        self.previous: dict[str, list[tuple[str, str, str | None]]] = {}
        self.sources: dict[str, str] = {}
        self.desired: dict[str, set[str]] = {}
        self.touched: set[str] = set()
        self.origins: dict[str, tuple[str, str | None]] = {}
        for label, kind in _table_index(engine).items():
            if kind != "NODE" or "_document_state" not in _table_columns(engine, label):
                continue
            columns = _table_columns(engine, label)
            for prop in ("_document_identity", "_document_source", "_document_file"):
                if prop not in columns:
                    engine.execute_write(
                        f"ALTER TABLE {label} ADD {prop} STRING DEFAULT CAST(NULL AS STRING)"
                    )
            for key, source, identity, canonical, file in engine.execute(
                f"MATCH (n:{label}) RETURN n.id,n._source,n._document_identity,n._document_source,n._document_file"
            ).rows:
                import re

                base = identity or re.sub(r"~\d{4}$", "", str(key).split("#", 1)[0])
                if not identity and not re.fullmatch(r".+-[0-9a-f]{64}", base):
                    continue  # unknown/authored keys are not loader-owned documents
                normalized = canonical or (_normalized_source(source) if source else "")
                if normalized:
                    self.sources.setdefault(normalized, base)
                self.previous.setdefault(base, []).append((label, key, file))
                if file and any(self._within(file, p) for p in req.sync_paths):
                    self.touched.add(base)

    @staticmethod
    def _within(file: str, path: str) -> bool:
        from pathlib import Path

        return Path(file).is_relative_to(Path(path))

    def identity(self, doc) -> str:
        from grag.ingest.loaders import _normalized_source, _source_identity

        canonical = _normalized_source(doc.source)
        base = self.sources.get(canonical) or _source_identity(
            doc.source, doc.text, doc.metadata
        )
        self.touched.add(base)
        previous_file = next(
            (file for _, _, file in self.previous.get(base, []) if file), None
        )
        self.origins[base] = (canonical, doc.source_file or previous_file)
        if canonical:
            self.sources[canonical] = base
        return base

    def mark(self, nodes) -> None:
        for node in nodes:
            key = str(node.key)
            self.desired.setdefault(node.label, set()).add(key)
            base = next(
                b
                for b in self.touched
                if key == b or key.startswith((b + "#", b + "~"))
            )
            source, file = self.origins[base]
            self.engine.execute_write(
                f"MATCH (n:{node.label} {{id:$key}}) SET n._document_identity=$identity, "
                "n._document_source=$source, n._document_file=$file, n._document_state='current'",
                {"key": key, "identity": base, "source": source, "file": file},
            )

    def replace_edges(self, warnings: list[str]) -> None:
        owners = set(self.touched)
        for base in self.touched:
            for _, key, _ in self.previous.get(base, []):
                owners.add(key.split("#", 1)[0])
        tables = [
            t
            for t, kind in _table_index(self.engine).items()
            if kind == "REL" and DOCUMENT_OWNER_PROP in _table_columns(self.engine, t)
        ]
        replace_owned_edges(self.engine, tables, sorted(owners), warnings)

    def prune(self, warnings: list[str]) -> int:
        stale: dict[str, list[str]] = {}
        for base in self.touched:
            for label, key, _ in self.previous.get(base, []):
                if key not in self.desired.get(label, set()):
                    stale.setdefault(label, []).append(key)
        return sum(
            prune_unreferenced_nodes(
                self.engine, label, "n.id IN $keys", {"keys": keys}, warnings
            )
            for label, keys in stale.items()
        )
