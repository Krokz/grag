"""Budgeted context assembly for explicit node sets (get_context tool path)."""

from __future__ import annotations

from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine, node_record_from_value
from grag.core.errors import SchemaError
from grag.core.types import (
    ContextRequest,
    ContextResponse,
    NodeRecord,
    Subgraph,
    merge_subgraphs,
    split_node_id,
)
from grag.retrieval.search import _expand_neighborhood, _pack
from grag.retrieval.vectors import _ident, node_tables, pk_map_with_fallback


def _resolve_bare_key(
    engine: Engine, pk: dict[str, str], known: set[str], nid: str
) -> tuple[str, str] | None:
    """Resolve a node id whose 'label' segment isn't a real table.

    Code-graph pks look like 'repo:path#qual' (no 'Label:' prefix), so
    split_node_id misreads the repo as the label. Probe each searchable table
    for a row whose primary key equals the whole id; return (label, key) for a
    unique hit, else None (caller raises unknown-label). Ambiguous across
    tables resolves to None too — callers should pass the 'Label:' prefix.
    """
    hits: list[str] = []
    for label in sorted(known):
        p = pk.get(label)
        if not p:
            continue
        res = engine.execute(
            f"MATCH (n:{_ident(label)}) WHERE n.{_ident(p)} = $k RETURN n.{_ident(p)}",
            {"k": nid},
        )
        if res.rows:
            hits.append(label)
    if len(hits) == 1:
        return hits[0], nid
    return None


def get_context(
    engine: Engine, config: GragConfig, req: ContextRequest
) -> ContextResponse:
    """Look up req.node_ids ('Label:key'), expand k hops, and pack the result
    into a token budget. Node ids that don't resolve are excluded; unknown
    *labels* are a SchemaError."""
    hops = max(0, min(req.hops, config.max_hops))
    budget = req.token_budget or config.default_token_budget
    pk = pk_map_with_fallback(engine)
    known = set(node_tables(engine))

    groups: dict[str, list[str]] = {}
    normalized: list[tuple[str, str]] = []  # (label, key) per input id, resolved
    for nid in req.node_ids:
        label, key = split_node_id(nid)
        if not label or not key:
            raise SchemaError(
                f"Invalid node id {nid!r}.",
                hint="Node ids look like 'Label:key' (see make_node_id).",
            )
        if label not in known:
            # Code-graph node keys are themselves 'repo:path#qual' (no Label
            # prefix), so split_node_id reads the repo as the label. Treat the
            # whole id as a bare key and resolve its label from the tables.
            resolved = _resolve_bare_key(engine, pk, known, nid)
            if resolved is not None:
                label, key = resolved
            else:
                raise SchemaError(
                    f"Unknown label '{label}' (from node id {nid!r}).",
                    hint=f"Available node labels: {sorted(known)}. Use describe_schema for details.",
                )
        groups.setdefault(label, []).append(key)
        normalized.append((label, key))

    found: dict[tuple[str, str], tuple[NodeRecord, Any]] = {}
    for label, keys in groups.items():
        p = pk.get(label)
        if not p:
            raise SchemaError(
                f"Table '{label}' has no known primary key.",
                hint="Define the table via define_schema so node ids can resolve.",
            )
        res = engine.execute(
            f"MATCH (n:{_ident(label)}) WHERE n.{_ident(p)} IN $keys RETURN n",
            {"keys": keys},
        )
        for (nv,) in res.rows:
            found[(label, str(nv.get(p)))] = (node_record_from_value(nv, pk), nv.get(p))

    seeds: list[NodeRecord] = []
    refs: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for label, key in normalized:
        hit = found.get((label, key))
        if hit is None or hit[0].id in seen:
            continue
        seen.add(hit[0].id)
        seeds.append(hit[0])
        refs.append((label, hit[1]))

    expanded = _expand_neighborhood(engine, refs, hops, pk)
    subgraph = merge_subgraphs(Subgraph(nodes=seeds), expanded)
    packed = _pack(subgraph, budget, [n.id for n in seeds])
    return ContextResponse(
        context=packed.text,
        token_estimate=packed.token_estimate,
        included_node_ids=packed.included_node_ids,
        truncated=packed.truncated,
        subgraph=subgraph,
    )
