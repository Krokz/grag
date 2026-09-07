"""Subgraph serialization: pack a Subgraph into compact, cited, token-budgeted
text for LLM prompt injection.

Line formats:
    node:  Doc:doc-0 {title: "graph databases", text: "..."} [source: file.md]
    edge:  Doc:doc-0 -[RELATED {since: 2024}]-> Doc:doc-1

Bulky vector payloads (VECTOR_PROPS) are never rendered; `_source` becomes the
[source: ...] citation suffix instead of an inline property.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable
from typing import Any

from grag.core.types import (
    PROVENANCE_SOURCE,
    VECTOR_PROPS,
    EdgeRecord,
    FreshnessReport,
    NodeRecord,
    PackedContext,
    Subgraph,
)


def estimate_tokens(text: str) -> int:
    """Deterministic size estimate, not a model-specific tokenizer count."""
    return (len(text.encode("utf-8")) + 3) // 4


def with_freshness(text: str, freshness: FreshnessReport) -> str:
    footer = json.dumps({"freshness": freshness.model_dump()}, separators=(",", ":"))
    return f"{text}\n\n---\n{footer}" if text else footer


def _render_value(value: Any) -> str:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        value = value.isoformat()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _render_props(props: dict[str, Any], skip: set[str]) -> str:
    parts = [
        f"{k}: {_render_value(v)}"
        for k, v in props.items()
        if k not in skip and k not in VECTOR_PROPS and v is not None
    ]
    return "{" + ", ".join(parts) + "}" if parts else ""


def _node_line(node: NodeRecord) -> str:
    line = node.id
    rendered = _render_props(node.properties, skip={PROVENANCE_SOURCE})
    if rendered:
        line += f" {rendered}"
    source = node.properties.get(PROVENANCE_SOURCE)
    if source:
        if isinstance(source, str) and any(c in source for c in "\n\r\t[]"):
            source = _render_value(source)
        line += f" [source: {source}]"
    return line


def _edge_line(edge: EdgeRecord) -> str:
    rendered = _render_props(edge.properties, skip=set())
    inner = edge.type + (f" {rendered}" if rendered else "")
    return f"{edge.source} -[{inner}]-> {edge.target}"


def _ordered_nodes(subgraph: Subgraph, seed_ids: list[str] | None) -> list[NodeRecord]:
    by_id = subgraph.node_map()
    ordered: list[NodeRecord] = []
    seen: set[str] = set()
    for nid in seed_ids or []:
        node = by_id.get(nid)
        if node is not None and nid not in seen:
            seen.add(nid)
            ordered.append(node)
    for node in subgraph.nodes:
        if node.id not in seen:
            seen.add(node.id)
            ordered.append(node)
    return ordered


def pack_context(
    subgraph: Subgraph,
    token_budget: int,
    seed_ids: list[str] | None = None,
    *,
    measure: Callable[[PackedContext], int] | None = None,
) -> PackedContext:
    """Pack complete values and coherent edges, with explicit omission counts.

    Retrieval supplies a measure that includes its entire response envelope.
    Under pressure, reserve half the available space for seed-first topology,
    then fill properties (citations first). No string, citation, or nested value
    is silently shortened. An oversized STRING can be read through text paging.
    """

    def public(props: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in props.items() if k not in VECTOR_PROPS and v is not None
        }

    nodes = [
        n.model_copy(update={"properties": public(n.properties)})
        for n in _ordered_nodes(subgraph, seed_ids)
    ]
    ids = {n.id for n in nodes}
    edges = [
        e.model_copy(update={"properties": public(e.properties)})
        for e in subgraph.edges
        if e.source in ids and e.target in ids
    ]
    originals = {n.id: n.properties for n in nodes}
    original_edges = {e.id: e.properties for e in edges}

    def build(ns: list[NodeRecord], es: list[EdgeRecord]) -> PackedContext:
        text = "\n".join([*(_node_line(n) for n in ns), *(_edge_line(e) for e in es)])
        omitted_nodes = len(nodes) - len(ns)
        omitted_edges = len(subgraph.edges) - len(es)
        omitted_props = sum(len(originals[n.id]) - len(n.properties) for n in ns)
        omitted_props += sum(len(original_edges[e.id]) - len(e.properties) for e in es)
        return PackedContext(
            text=text,
            token_estimate=estimate_tokens(text),
            included_node_ids=[n.id for n in ns],
            truncated=bool(omitted_nodes or omitted_edges or omitted_props),
            omitted_nodes=omitted_nodes,
            omitted_edges=omitted_edges,
            omitted_properties=omitted_props,
            subgraph=Subgraph(nodes=ns, edges=es),
        )

    cost = measure or (lambda packed: packed.token_estimate)
    full = build(nodes, edges)
    if cost(full) <= token_budget:
        return full
    kept_nodes: list[NodeRecord] = []
    kept_edges: list[EdgeRecord] = []
    empty = build([], [])
    if token_budget <= cost(empty):
        return empty
    topology_budget = cost(empty) + (token_budget - cost(empty)) // 2
    kept_ids: set[str] = set()
    edge_ids: set[str] = set()
    adjacent: dict[str, list[EdgeRecord]] = {}
    for edge in edges:
        adjacent.setdefault(edge.source, []).append(edge)
        if edge.target != edge.source:
            adjacent.setdefault(edge.target, []).append(edge)
    for node in nodes:
        skeleton = node.model_copy(update={"properties": {}})
        candidate = build([*kept_nodes, skeleton], kept_edges)
        # A long first id may need more than the topology share on its own.
        limit = topology_budget if kept_nodes else token_budget
        if cost(candidate) > limit:
            continue
        kept_nodes.append(skeleton)
        kept_ids.add(node.id)
        for edge in adjacent.get(node.id, []):
            if edge.id in edge_ids or not {edge.source, edge.target} <= kept_ids:
                continue
            bare_edge = edge.model_copy(update={"properties": {}})
            if cost(build(kept_nodes, [*kept_edges, bare_edge])) <= topology_budget:
                kept_edges.append(bare_edge)
                edge_ids.add(edge.id)

    # Long seed ids can exhaust the topology share before a neighbor fits.
    # Give the first connecting pair a chance before filling node properties.
    if not kept_edges:
        by_id = {n.id: n for n in nodes}
        candidates = (e for n in kept_nodes for e in adjacent.get(n.id, []))
        for edge in candidates:
            missing = dict.fromkeys(
                nid for nid in (edge.source, edge.target) if nid not in kept_ids
            )
            neighbors = [
                by_id[nid].model_copy(update={"properties": {}}) for nid in missing
            ]
            bare_edge = edge.model_copy(update={"properties": {}})
            if cost(build([*kept_nodes, *neighbors], [bare_edge])) <= token_budget:
                kept_nodes.extend(neighbors)
                kept_edges.append(bare_edge)
                break

    # Every provenance citation gets a chance before long bodies consume space.
    for node in kept_nodes:
        if PROVENANCE_SOURCE in originals[node.id]:
            node.properties[PROVENANCE_SOURCE] = originals[node.id][PROVENANCE_SOURCE]
            if cost(build(kept_nodes, kept_edges)) > token_budget:
                del node.properties[PROVENANCE_SOURCE]
    priority = {
        key: i for i, key in enumerate(("title", "name", "summary", "body", "text"))
    }
    records: list[tuple[NodeRecord | EdgeRecord, dict[str, Any]]] = [
        *((n, originals[n.id]) for n in kept_nodes),
        *((e, original_edges[e.id]) for e in kept_edges),
    ]
    for record, props in records:
        for key in sorted(
            props, key=lambda k: (k.startswith("_"), priority.get(k, 5), k)
        ):
            if key in record.properties:
                continue
            record.properties[key] = props[key]
            if cost(build(kept_nodes, kept_edges)) > token_budget:
                del record.properties[key]
    return build(kept_nodes, kept_edges)
