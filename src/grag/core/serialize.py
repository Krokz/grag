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
from typing import Any

from grag.core.types import (
    PROVENANCE_SOURCE,
    VECTOR_PROPS,
    EdgeRecord,
    NodeRecord,
    PackedContext,
    Subgraph,
)

_MAX_STRING = 200


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def _render_value(value: Any) -> str:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        value = value.isoformat()
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            value = value[: _MAX_STRING - 3] + "..."
        return json.dumps(value, ensure_ascii=False)
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
    subgraph: Subgraph, token_budget: int, seed_ids: list[str] | None = None
) -> PackedContext:
    included_node_ids: list[str] = []
    truncated = False

    pending_nodes = _ordered_nodes(subgraph, seed_ids)
    current = ""

    def fits(line: str) -> bool:
        candidate = f"{current}\n{line}" if current else line
        return estimate_tokens(candidate) <= token_budget

    for node in pending_nodes:
        line = _node_line(node)
        if fits(line):
            current = f"{current}\n{line}" if current else line
            included_node_ids.append(node.id)
        else:
            truncated = True

    for edge in subgraph.edges:
        line = _edge_line(edge)
        if fits(line):
            current = f"{current}\n{line}" if current else line
        else:
            truncated = True

    if token_budget <= 0 and subgraph.nodes:
        truncated = True

    return PackedContext(
        text=current,
        token_estimate=estimate_tokens(current),
        included_node_ids=included_node_ids,
        truncated=truncated,
    )
