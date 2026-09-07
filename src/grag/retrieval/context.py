"""Budgeted context assembly for explicit node sets (get_context tool path)."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine, node_record_from_value
from grag.core.errors import NotFoundError, SchemaError
from grag.core.evidence import exclusion_reason, read_history, read_revision
from grag.core.limits import bounded_work
from grag.core.types import (
    ContextRequest,
    ContextResponse,
    FreshnessReport,
    NodeRecord,
    Subgraph,
    merge_subgraphs,
    split_node_id,
)
from grag.retrieval.packing import (
    pack_context_response,
    pack_text_page,
    retrieval_budget,
)
from grag.retrieval.search import _expand_neighborhood
from grag.retrieval.vectors import (
    _ident,
    node_tables,
    pk_map_with_fallback,
    table_properties,
)


def _projected_text_page(engine: Engine, label: str, key: str, pk: dict[str, str],
                         req: ContextRequest, budget: int, freshness: FreshnessReport | None) -> ContextResponse:
    """Hash in bounded projections and fetch only the requested text window.

    Serialize with managed writes for a consistent value while hashing. Native
    work remains subject to the statement timeout; Python never loads the full
    value or the node's unrelated properties/vectors for this page.
    """
    from grag.core.evidence import SAFETY_PROPS

    columns = table_properties(engine, label)
    prop = req.text_property
    if prop is None or columns.get(prop) != "STRING":
        raise SchemaError("Text paging requires a declared STRING property.")
    p = _ident(pk[label])
    prop = _ident(prop)
    match = f"MATCH (n:{_ident(label)}) WHERE n.{p}=$key "
    selected = sorted(({p, "_source", *SAFETY_PROPS} & set(columns)) - ({prop} if prop not in SAFETY_PROPS else set()))
    projection = ", ".join(f"n.{_ident(name)}" for name in selected)
    with engine.serialized_writes():
        rows = engine.execute(match + f"RETURN size(n.{prop})" + (f", {projection}" if projection else ""), {"key": key}).rows
        if not rows:
            raise NotFoundError("Node not found for text paging.")
        total, *values = rows[0]
        if total is None:
            raise SchemaError("Unknown or non-text property: the selected value is null.")
        node = NodeRecord(id=f"{label}:{key}", label=label,
                          properties=dict(zip(selected, values, strict=True)))
        if req.evidence == "current" and exclusion_reason(node, dt.datetime.now(dt.timezone.utc)):
            raise NotFoundError("Node excluded by the evidence policy for text paging.",
                                hint="Use evidence='all' to inspect obsolete/disputed evidence.")
        digest = hashlib.sha256()
        # Stream at most 64K characters at once, including Unicode boundaries.
        for offset in range(0, total, 65_536):
            part = engine.execute(match + f"RETURN substring(n.{prop}, $start, $length)",
                                  {"key": key, "start": offset + 1, "length": min(65_536, total - offset)}).rows[0][0]
            digest.update(part.encode("utf-8"))
        if req.text_sha256 is not None and req.text_sha256 != digest.hexdigest():
            raise NotFoundError("Text changed since the previous page.",
                                hint="Restart with text_offset=0 and omit text_sha256.")
        if req.text_offset > total:
            raise SchemaError("text_offset is beyond the end of the STRING.")
        text = engine.execute(match + f"RETURN substring(n.{prop}, $start, $length)",
                              {"key": key, "start": req.text_offset + 1, "length": min(4 * budget, total - req.text_offset)}).rows[0][0]
        node.properties[prop] = text
        return pack_text_page(node, req, budget, freshness=freshness,
                              page_origin=req.text_offset, page_total=total, page_digest=digest.hexdigest())


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


@bounded_work
def get_context(
    engine: Engine, config: GragConfig, req: ContextRequest, *, freshness: FreshnessReport | None = None,
) -> ContextResponse:
    """Look up req.node_ids ('Label:key'), expand k hops, and pack the result
    into a token budget. Node ids that don't resolve are excluded; unknown
    *labels* are a SchemaError."""
    hops = max(0, min(req.hops, config.max_hops))
    budget = retrieval_budget(req.token_budget, config.default_token_budget)
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

    # History reads don't need to load the current entity's large properties.
    if req.history or req.revision is not None:
        from grag.core.types import make_node_id

        label, key = normalized[0]
        identity = make_node_id(label, key)
        if req.history:
            return read_history(engine, identity, req, budget, freshness)
        if req.revision is None:
            raise SchemaError("A historical snapshot requires revision.")
        historical = read_revision(engine, identity, req.revision, pk)
        if req.text_property is not None:
            return pack_text_page(historical, req, budget, freshness=freshness)
        return pack_context_response(Subgraph(nodes=[historical]), budget, [historical.id],
                                     freshness=freshness, evidence_policy="all")
    if req.text_property is not None:
        label, key = normalized[0]
        return _projected_text_page(engine, label, key, pk, req, budget, freshness)

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

    now = dt.datetime.now(dt.timezone.utc)
    excluded = {node.id for node in seeds if exclusion_reason(node, now)} if req.evidence == "current" and req.revision is None else set()
    seeds = [node for node in seeds if node.id not in excluded]
    refs = [(label, key) for label, key in refs if f"{label}:{key}" not in excluded]

    if req.text_property is not None:
        if not seeds:
            raise NotFoundError(
                "Node not found or excluded by the evidence policy for text paging.",
                hint="Use a current canonical id, or evidence='all' to inspect obsolete/disputed evidence.",
            )
        return pack_text_page(seeds[0], req, budget, freshness=freshness)

    expanded, expansion_limited = _expand_neighborhood(engine, refs, hops, pk,
        excluded=excluded if req.evidence == "current" else None, now=now)
    subgraph = merge_subgraphs(Subgraph(nodes=seeds), expanded)
    return pack_context_response(
        subgraph,
        budget,
        [n.id for n in seeds],
        expansion_limited=expansion_limited,
        freshness=freshness,
        evidence_policy="all" if req.revision is not None else req.evidence,
        excluded_evidence=len(excluded),
    )
