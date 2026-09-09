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

from grag.core.limits import bounded_work, charge
from grag.core.types import (
    PROVENANCE_SOURCE,
    VECTOR_PROPS,
    EdgeRecord,
    FreshnessReport,
    NodeRecord,
    PackedContext,
    Subgraph,
    TextExcerpt,
)

# Kept here without importing evidence.py, which depends on the engine.
_SAFETY_PROPS = {"_evidence_state", "_review_state", "_expires_at", "_superseded_by",
                 "_evidence_seq", "_document_state", "_source_state", "_history_revision", "_evidence_visibility", "status"}
_CITATION_PROPS = {PROVENANCE_SOURCE, "line_start", "line_end", "status", *_SAFETY_PROPS}


def estimate_tokens(text: str) -> int:
    """Deterministic size estimate, not a model-specific tokenizer count."""
    return (len(text.encode("utf-8")) + 3) // 4


def with_freshness(text: str, freshness: FreshnessReport) -> str:
    footer = json.dumps({"freshness": freshness.model_dump()}, separators=(",", ":"))
    return f"{text}\n\n---\n{footer}" if text else footer


def compact_graph_values(value: Any) -> Any:
    """Omit null columns only on whole native entities in MCP query output.

    Explicit projections, including user-built maps/lists containing null,
    remain exact. Identity, revisions and every non-null property are retained.
    """
    if isinstance(value, dict):
        entity = "_ID" in value and "_LABEL" in value
        return {k: compact_graph_values(v) for k, v in value.items() if not (entity and v is None)}
    if isinstance(value, list):
        return [compact_graph_values(v) for v in value]
    return value


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


@bounded_work
def pack_context(
    subgraph: Subgraph,
    token_budget: int,
    seed_ids: list[str] | None = None,
    *,
    measure: Callable[[PackedContext], int] | None = None,
    excerpts: list[TextExcerpt] | None = None,
) -> PackedContext:
    """Pack complete values and coherent edges, with explicit omission counts.

    Retrieval supplies a measure that includes its entire response envelope.
    Under pressure, reserve half the available space for seed-first topology,
    then fill citations and cross-node evidence before secondary metadata.
    Property values stay whole. Optional exact excerpts are separately marked
    partial evidence; the omitted full STRING remains available through paging.
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

    kept_excerpts: list[TextExcerpt] = []

    def build(ns: list[NodeRecord], es: list[EdgeRecord]) -> PackedContext:
        charge("packing_steps")
        text = "\n".join([
            *(_node_line(n) for n in ns),
            *(f"{e.node_id} [excerpt {e.property} chars {e.offset}:{e.end} of {e.total_chars}] "
              f"{_render_value(e.text)}" for e in kept_excerpts),
            *(_edge_line(e) for e in es),
        ])
        charge("packing_bytes", len(text.encode("utf-8")))
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
            text_excerpts=list(kept_excerpts),
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

    def skeleton(node: NodeRecord) -> NodeRecord:
        # Admit a usable unit of evidence, not a bare ID whose citation will
        # consume the entire remaining budget later. Keep lifecycle status
        # alongside prose without deciding which statuses are retrievable.
        props = {k: v for k, v in node.properties.items()
                 if k in _CITATION_PROPS}
        for key in ("body", "text", "summary", "rationale", "docstring", "description"):
            value = node.properties.get(key)
            if isinstance(value, str) and len(value) <= 512:
                props[key] = value
                break
        return node.model_copy(update={"properties": props})

    kept_ids: set[str] = set()
    edge_ids: set[str] = set()
    adjacent: dict[str, list[EdgeRecord]] = {}
    for edge in edges:
        adjacent.setdefault(edge.source, []).append(edge)
        if edge.target != edge.source:
            adjacent.setdefault(edge.target, []).append(edge)
    for node in nodes:
        projected = skeleton(node)
        candidate = build([*kept_nodes, projected], kept_edges)
        # A long first id may need more than the topology share on its own.
        limit = topology_budget if kept_nodes else token_budget
        if cost(candidate) > limit:
            if kept_nodes:
                continue
            if _SAFETY_PROPS.intersection(node.properties):
                continue  # never strip lifecycle qualifiers to admit old prose
            projected = node.model_copy(update={"properties": {}})
            if cost(build([projected], kept_edges)) > token_budget:
                continue
        kept_nodes.append(projected)
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
                skeleton(by_id[nid]) for nid in missing
            ]
            bare_edge = edge.model_copy(update={"properties": {}})
            if cost(build([*kept_nodes, *neighbors], [bare_edge])) <= token_budget:
                kept_nodes.extend(neighbors)
                kept_edges.append(bare_edge)
                break

    # A code citation needs its line range as well as its path. Admit the
    # bundle together, before any body's or housekeeping field's allocation.
    for node in kept_nodes:
        citation = {k: v for k, v in originals[node.id].items()
                    if k in _CITATION_PROPS}
        node.properties.update(citation)
        if cost(build(kept_nodes, kept_edges)) > token_budget:
            node.properties.clear()
    priority = {
        key: i for i, key in enumerate(("body", "text", "summary", "rationale", "docstring",
                                       "description", "title", "name", "status", "signature"))
    }
    secondary = {"id", "path", "language", "is_method", "meta", "heading_path", "ingested_at", "git_commit", "git_branch"}
    excerpt_map: dict[tuple[str, str], list[TextExcerpt]] = {}
    for excerpt in excerpts or []:
        excerpt_map.setdefault((excerpt.node_id, excerpt.property), []).append(excerpt)

    def fill(records: list[NodeRecord | EdgeRecord], *, metadata: bool = False) -> None:
        fields = sorted(
            ((record, key, value)
             for record in records
             for key, value in (originals[record.id] if isinstance(record, NodeRecord) else original_edges[record.id]).items()
             if (key.startswith("_") or key in secondary) == metadata),
            key=lambda item: priority.get(item[1], 10),
        )
        for record, key, value in fields:
            if isinstance(record, NodeRecord) and any(k not in record.properties for k in _SAFETY_PROPS.intersection(originals[record.id])):
                continue
            if key in record.properties:
                continue
            if isinstance(record, NodeRecord) and key in _CITATION_PROPS:
                continue  # already admitted as one citation/status bundle
            record.properties[key] = value
            if cost(build(kept_nodes, kept_edges)) <= token_budget:
                continue
            del record.properties[key]
            if isinstance(record, NodeRecord):
                if originals[record.id].get(PROVENANCE_SOURCE) and not record.properties.get(PROVENANCE_SOURCE):
                    continue
                for excerpt in excerpt_map.get((record.id, key), []):
                    kept_excerpts.append(excerpt)
                    if cost(build(kept_nodes, kept_edges)) <= token_budget:
                        break
                    kept_excerpts.pop()

    # Cross-record evidence comes before one seed's repeated ID/hash/date.
    fill([*kept_nodes, *kept_edges])
    kept_ids = {n.id for n in kept_nodes}
    edge_ids = {e.id for e in kept_edges}

    def connect() -> None:
        for edge in edges:
            if edge.id in edge_ids or not {edge.source, edge.target} <= kept_ids:
                continue
            bare = edge.model_copy(update={"properties": {}})
            if cost(build(kept_nodes, [*kept_edges, bare])) <= token_budget:
                kept_edges.append(bare)
                edge_ids.add(edge.id)
                fill([bare])

    connect()
    # Spend remaining room on additional cited evidence and its connecting
    # edge before housekeeping. This makes the topology share a reservation,
    # not a permanent cap that strands useful neighbors outside the answer.
    for node in nodes:
        if node.id in kept_ids:
            continue
        links = [e for e in adjacent.get(node.id, [])
                 if {e.source, e.target} <= kept_ids | {node.id}]
        if seed_ids and node.id not in seed_ids and adjacent.get(node.id) and not links:
            continue
        projected = skeleton(node)
        added = [links[0].model_copy(update={"properties": {}})] if links else []
        if cost(build([*kept_nodes, projected], [*kept_edges, *added])) > token_budget:
            continue
        kept_nodes.append(projected)
        kept_ids.add(node.id)
        kept_edges.extend(added)
        edge_ids.update(e.id for e in added)
        fill([projected, *added])
        connect()
    fill([*kept_nodes, *kept_edges], metadata=True)
    return build(kept_nodes, kept_edges)
