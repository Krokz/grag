"""Validate and commit a complete upsert, with optional durable retry receipts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConflictError, NotFoundError, SchemaError
from grag.core.ident import validate_identifier
from grag.core.mutate import (
    _coerce_value,
    _node_pks,
    _rel_endpoints,
    _sanitize_props,
    _table_columns,
    _table_index,
    _upsert_edges,
    _upsert_nodes,
)
from grag.core.revisions import canonical_json, content_revision
from grag.core.types import (
    MutationSummary,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
    make_node_id,
)

OPERATIONS_TABLE = "_grag_operations"


@dataclass
class Target:
    query: str
    params: dict[str, Any]
    identity: str
    expected: str | None

    def revision(self, engine: Engine) -> str:
        rows = engine.execute(self.query, self.params).rows
        if len(rows) > 1:
            raise SchemaError(
                f"Multiple relationships have identity {self.identity}; reconcile duplicates before upserting"
            )
        return content_revision(rows[0][0]) if rows else "absent"


def _key(label: str, value: Any, pks: dict, columns: dict) -> Any:
    pk = pks.get(label)
    if pk is None:
        raise SchemaError(
            f"Unknown node label or missing primary key: {label}",
            hint="Call define_schema for this node label first; inspect describe_schema.",
        )
    validate_identifier(label)
    validate_identifier(pk)
    ok, coerced = _coerce_value(columns[pk], value)
    if value is None or not ok or isinstance(coerced, (list, dict, tuple, set)):
        raise SchemaError(
            f"Invalid primary key for {label}.{pk}: expected non-null {columns[pk]}"
        )
    return coerced


def _prepare(
    engine: Engine, req: UpsertNodesRequest | UpsertEdgesRequest
) -> tuple[list[UpsertNode], list[UpsertEdge], list[Target], list[str]]:
    pks = _node_pks(engine)
    rels = _rel_endpoints(engine)
    cache: dict[str, dict] = {}

    def columns(label: str) -> dict:
        validate_identifier(label)
        if label not in cache:
            cache[label] = _table_columns(engine, label)
        return cache[label]

    def node_key(label: str, key: Any) -> Any:
        if label not in pks:
            raise SchemaError(
                f"Unknown node label '{label}'",
                hint="Call define_schema first; inspect describe_schema for existing labels.",
            )
        return _key(label, key, pks, columns(label))

    warnings: list[str] = []
    targets: list[Target] = []
    nodes: list[UpsertNode] = []
    pending: list[tuple[str, Any]] = []
    for node in req.nodes if isinstance(req, UpsertNodesRequest) else []:
        key = node_key(node.label, node.key)
        pk = pks[node.label]
        if pk in node.properties:
            raise SchemaError(
                f"Primary key {node.label}.{pk} belongs in key, not properties",
                hint="Remove the primary-key property and retry the complete batch.",
            )
        _, _, accepted = _sanitize_props(
            props=node.properties,
            columns=columns(node.label),
            alias="n",
            owner=f"Node {node.label}:{key}",
            warnings=warnings,
        )
        nodes.append(node.model_copy(update={"key": key, "properties": accepted}))
        pending.append((node.label, key))
        targets.append(
            Target(
                f"MATCH (n:{node.label} {{{pk}: $key}}) RETURN n",
                {"key": key},
                make_node_id(node.label, key),
                node.expected_revision,
            )
        )

    edges: list[UpsertEdge] = []
    checked: list[tuple[str, Any]] = []
    for edge in req.edges:
        if edge.type not in rels:
            raise SchemaError(
                f"Unknown rel type '{edge.type}'",
                hint="Call define_schema for this relationship first.",
            )
        if (edge.from_label, edge.to_label) != rels[edge.type]:
            raise SchemaError(
                f"Rel '{edge.type}' connects {rels[edge.type]}, not ({edge.from_label}, {edge.to_label})",
                hint=f"Use from_label='{rels[edge.type][0]}' and to_label='{rels[edge.type][1]}', or define a separate relationship.",
            )
        fk, tk = (
            node_key(edge.from_label, edge.from_key),
            node_key(edge.to_label, edge.to_key),
        )
        for label, key in ((edge.from_label, fk), (edge.to_label, tk)):
            if (label, key) in pending or (label, key) in checked:
                continue
            checked.append((label, key))
            if not engine.execute(
                f"MATCH (n:{label} {{{pks[label]}: $key}}) RETURN n.{pks[label]} LIMIT 1",
                {"key": key},
            ).rows:
                raise NotFoundError(
                    f"Edge endpoint node does not exist: {label}:{key}",
                    hint="Include the node in this upsert_nodes batch, or call upsert_nodes first.",
                )
        _, _, accepted = _sanitize_props(
            props=edge.properties,
            columns=columns(edge.type),
            alias="r",
            owner=f"Edge {edge.type}({edge.from_label}:{fk}->{edge.to_label}:{tk})",
            warnings=warnings,
        )
        edges.append(
            edge.model_copy(
                update={"from_key": fk, "to_key": tk, "properties": accepted}
            )
        )
        targets.append(
            Target(
                f"MATCH (a:{edge.from_label} {{{pks[edge.from_label]}: $fk}})-[r:{edge.type}]->(b:{edge.to_label} {{{pks[edge.to_label]}: $tk}}) RETURN r",
                {"fk": fk, "tk": tk},
                f"{edge.type}:{make_node_id(edge.from_label, fk)}->{make_node_id(edge.to_label, tk)}",
                edge.expected_revision,
            )
        )
    # Every guard checks the state before this request's first write, including
    # repeated identities. Duplicate unguarded patches retain their input order.
    for target in targets:
        if target.expected is not None and target.revision(engine) != target.expected:
            raise ConflictError(
                f"Revision conflict for {target.identity}; nothing from this operation was saved",
                code="revision_conflict",
                hint="Read the current entity, reconcile the edit, and retry with its _revision and a new operation_id. Use expected_revision='absent' for create-only writes.",
            )
    return nodes, edges, targets, warnings


def _receipt(engine: Engine, operation_id: str, digest: str) -> MutationSummary | None:
    if OPERATIONS_TABLE not in _table_index(engine):
        return None
    rows = engine.execute(
        f"MATCH (o:{OPERATIONS_TABLE} {{id: $id}}) RETURN o.digest, o.result",
        {"id": operation_id},
    ).rows
    if not rows:
        return None
    if rows[0][0] != digest:
        raise ConflictError(
            f"operation_id {operation_id!r} was already used with a different request",
            code="operation_id_conflict",
            hint="Retry an ambiguous result with exactly the original request and operation_id. Use a new ID for a different intended write.",
        )
    return MutationSummary.model_validate_json(rows[0][1]).model_copy(
        update={"replayed": True}
    )


def apply_mutation(
    engine: Engine, config: GragConfig, req: UpsertNodesRequest | UpsertEdgesRequest
) -> MutationSummary:
    # Retrying callers may reuse their model instance. Keep hashing, validation,
    # and publication bound to the same snapshot of its mutable input fields.
    req = req.model_copy(deep=True)
    with engine.serialized_writes():
        if req.operation_id and engine.in_write_transaction:
            with engine.atomic_writes():
                raise SchemaError(
                    "operation_id requires a top-level upsert; an enclosing transaction has not committed yet"
                )
        digest = None
        if req.operation_id:
            try:
                encoded = canonical_json(
                    {
                        "contract": "grag-upsert-v1",
                        "kind": type(req).__name__,
                        "request": req.model_dump(
                            mode="json", exclude={"operation_id"}
                        ),
                    }
                )
            except (ValueError, TypeError) as exc:
                raise SchemaError(
                    f"Retryable mutation must contain finite JSON values: {exc}"
                ) from exc
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            previous = _receipt(engine, req.operation_id, digest)
            if previous is not None:
                return previous

        # Validate before creating even the optional receipt table. When joined
        # to an ingest, Python validation errors must poison that transaction.
        with engine.atomic_writes():
            nodes, edges, targets, warnings = _prepare(engine, req)
            if req.operation_id and OPERATIONS_TABLE not in _table_index(engine):
                engine.execute_write(
                    f"CREATE NODE TABLE {OPERATIONS_TABLE}(id STRING PRIMARY KEY, digest STRING, result STRING)"
                )
            node_summary = (
                _upsert_nodes(engine, config, UpsertNodesRequest(nodes=nodes))
                if nodes
                else MutationSummary()
            )
            edge_summary = (
                _upsert_edges(engine, config, UpsertEdgesRequest(edges=edges))
                if edges
                else MutationSummary()
            )
            summary = MutationSummary(
                nodes=len(nodes),
                edges=len(edges),
                warnings=warnings + node_summary.warnings + edge_summary.warnings,
                operation_id=req.operation_id,
            )
            if req.operation_id or any(
                target.expected is not None for target in targets
            ):
                summary.revisions = {t.identity: t.revision(engine) for t in targets}
            if req.operation_id:
                engine.execute_write(
                    f"CREATE (o:{OPERATIONS_TABLE} {{id: $id, digest: $digest, result: $result}})",
                    {
                        "id": req.operation_id,
                        "digest": digest,
                        "result": summary.model_dump_json(),
                    },
                )
            return summary
