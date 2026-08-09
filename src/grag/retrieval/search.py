"""Hybrid retrieval: FTS + vector seeds, RRF fusion, k-hop expansion, and
token-budgeted context packing.

FTS indexes (one per searchable node table, named fts_index_name(table)) are
created lazily on first query and auto-maintained by LadybugDB afterwards.
Vector seeds are best-effort: any failure in the vector path degrades the
search to FTS-only rather than failing the request.
"""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any, Literal

from grag.config import GragConfig
from grag.core.engine import Engine, extract_subgraph, node_record_from_value
from grag.core.errors import GragError
from grag.core.types import (
    PackedContext,
    ScoredNode,
    SearchRequest,
    SearchResponse,
    Subgraph,
    fts_index_name,
    merge_subgraphs,
)
from grag.retrieval.vectors import (
    _ensure_extension,
    _ident,
    candidate_tables,
    pending_embedding_count,
    pk_map_with_fallback,
    string_props,
    vector_candidates,
)

log = logging.getLogger(__name__)

_RRF_K = 60
_MAX_EXPANSION_PATHS = 512  # per seed; bounds path enumeration on dense graphs


def search_knowledge(
    engine: Engine, config: GragConfig, req: SearchRequest
) -> SearchResponse:
    top_k = max(1, req.top_k)
    hops = max(0, min(req.hops, config.max_hops))
    budget = req.token_budget or config.default_token_budget
    pk = pk_map_with_fallback(engine)
    tables = candidate_tables(engine, config, req.labels)

    fts_list: list[ScoredNode] = []
    vec_list: list[ScoredNode] = []
    pending = 0
    if req.query.strip():
        for table in tables:
            fts_list.extend(_fts_seeds(engine, table, req.query, top_k, pk))
        try:
            vec_list = vector_candidates(engine, config, req.query, req.labels, top_k)
            pending = sum(pending_embedding_count(engine, config, t) for t in tables)
        except Exception as exc:  # noqa: BLE001 — vector path is best-effort
            log.warning("Vector search skipped, degrading to FTS-only: %s", exc)

    fused = _rrf_fuse({"fts": fts_list, "vector": vec_list})
    seeds = _diversify(fused, top_k, config.search_label_cap)
    seed_ids = [s.node.id for s in seeds]

    expanded = _expand_neighborhood(engine, _seed_refs(seeds, pk), hops, pk)
    subgraph = merge_subgraphs(Subgraph(nodes=[s.node for s in seeds]), expanded)
    packed = _pack(subgraph, budget, seed_ids)
    return SearchResponse(
        seeds=seeds, subgraph=subgraph, context=packed.text, pending_embeddings=pending
    )


# ---------------------------------------------------------------------------
# FTS seeds
# ---------------------------------------------------------------------------


_FTS_INDEXES: weakref.WeakKeyDictionary[Engine, set[str]] = weakref.WeakKeyDictionary()


def _fts_seeds(
    engine: Engine, table: str, query: str, top_k: int, pk: dict[str, str]
) -> list[ScoredNode]:
    cols = string_props(engine, table)
    if not cols:
        return []  # nothing indexable on this table
    _ensure_extension(engine, "FTS")
    index = fts_index_name(table)
    _ensure_fts_index(engine, table, index, cols)
    cypher = (
        f"CALL QUERY_FTS_INDEX('{_ident(table)}', '{index}', $q, TOP := {int(top_k)}) "
        "RETURN node, score"
    )
    try:
        res = engine.execute(cypher, {"q": query})
    except GragError:
        # reader may hold a stale catalog; the write connection is authoritative
        res = engine.execute_write(cypher, {"q": query})
    return [
        ScoredNode(node=node_record_from_value(nv, pk), score=float(score), match="fts")
        for nv, score in res.rows
    ]


def _ensure_fts_index(engine: Engine, table: str, index: str, cols: list[str]) -> None:
    """Create the FTS index once per engine. Create-first (duplicate error
    tolerated) because a failed QUERY_FTS_INDEX probe poisons the
    connection's catalog for that index name."""
    ensured = _FTS_INDEXES.setdefault(engine, set())
    if index in ensured:
        return
    col_list = "[" + ", ".join(f"'{c}'" for c in cols) + "]"
    try:
        engine.execute_write(
            f"CALL CREATE_FTS_INDEX('{_ident(table)}', '{index}', {col_list})"
        )
    except GragError as exc:
        if "already exists" not in str(exc):
            raise
    ensured.add(index)


# ---------------------------------------------------------------------------
# reciprocal rank fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(lists: dict[str, list[ScoredNode]]) -> list[ScoredNode]:
    """combined score = sum(1/(60 + rank)) across lists. The match label comes
    from whichever list ranked the node higher; 'fts' wins ties."""
    ranks: dict[str, dict[str, int]] = {}
    nodes: dict[str, Any] = {}
    for source, lst in lists.items():
        for i, scored in enumerate(lst):
            nid = scored.node.id
            nodes.setdefault(nid, scored.node)
            ranks.setdefault(nid, {})[source] = i + 1
    fused = []
    for nid, src_ranks in ranks.items():
        score = sum(1.0 / (_RRF_K + r) for r in src_ranks.values())
        fts_r = src_ranks.get("fts")
        vec_r = src_ranks.get("vector")
        match: Literal["fts", "vector"] = (
            "fts" if (fts_r is not None and (vec_r is None or fts_r <= vec_r)) else "vector"
        )
        fused.append(ScoredNode(node=nodes[nid], score=score, match=match))
    fused.sort(key=lambda s: s.score, reverse=True)
    return fused


def _diversify(
    fused: list[ScoredNode], top_k: int, cap: int
) -> list[ScoredNode]:
    """Cap how many fused top_k seeds one label may occupy. A label over the cap
    defers its extra seeds to a hold pool; after the main pass each label takes
    its best remaining held seeds (rank order) until top_k is filled. This keeps
    a large table (e.g. code Functions) from crowding out every other label
    while never dropping it entirely. cap <= 0 disables (pure RRF order)."""
    if cap <= 0:
        return fused[:top_k]
    held: list[ScoredNode] = []
    chosen: list[ScoredNode] = []
    counts: dict[str, int] = {}
    for s in fused:
        label = s.node.label
        if len(chosen) >= top_k:
            break
        if counts.get(label, 0) < cap:
            chosen.append(s)
            counts[label] = counts.get(label, 0) + 1
        else:
            held.append(s)
    # Backfill from held seeds (still fused-rank order) if we under-filled.
    for s in held:
        if len(chosen) >= top_k:
            break
        chosen.append(s)
    return chosen


# ---------------------------------------------------------------------------
# k-hop expansion
# ---------------------------------------------------------------------------


def _seed_refs(
    seeds: list[ScoredNode], pk: dict[str, str]
) -> list[tuple[str, Any]]:
    """(label, pk value) for each seed, deduped in fused order. Seeds whose
    table has no known pk are skipped (they still appear in the subgraph)."""
    refs: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for s in seeds:
        label = s.node.label
        p = pk.get(label)
        if not p:
            continue
        key = s.node.properties.get(p)
        if key is None:
            continue
        dedupe = (label, str(key))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        refs.append((label, key))
    return refs


def _expand_neighborhood(
    engine: Engine, seed_refs: list[tuple[str, Any]], hops: int, pk: dict[str, str]
) -> Subgraph:
    """Undirected k-hop neighborhood of the seed nodes, across all rel types."""
    if hops <= 0 or not seed_refs:
        return Subgraph()
    subs = []
    for label, key in seed_refs:
        p = pk.get(label)
        if not p:
            continue
        res = engine.execute(
            f"MATCH p = (a:{_ident(label)} {{{_ident(p)}: $key}})-[*1..{int(hops)}]-(b) "
            f"RETURN p LIMIT {_MAX_EXPANSION_PATHS}",
            {"key": key},
        )
        subs.append(extract_subgraph(res, pk))
    return merge_subgraphs(*subs) if subs else Subgraph()


# ---------------------------------------------------------------------------
# context packing (Agent A preferred, trivial fallback)
# ---------------------------------------------------------------------------


def _pack(
    subgraph: Subgraph, token_budget: int, seed_ids: list[str] | None = None
) -> PackedContext:
    try:
        from grag.core.serialize import pack_context
    except ImportError:
        return _fallback_pack_context(subgraph, token_budget, seed_ids)
    return pack_context(subgraph, token_budget, seed_ids=seed_ids)


def _fallback_pack_context(
    subgraph: Subgraph, token_budget: int, seed_ids: list[str] | None = None
) -> PackedContext:
    """Minimal line-per-record serializer, seeds first, ~4 chars per token."""
    node_map = subgraph.node_map()
    ordered = []
    seen = set()
    for nid in seed_ids or []:
        n = node_map.get(nid)
        if n is not None and nid not in seen:
            ordered.append(n)
            seen.add(nid)
    for n in sorted(subgraph.nodes, key=lambda x: x.id):
        if n.id not in seen:
            ordered.append(n)
            seen.add(n.id)

    def props_json(props: dict[str, Any]) -> str:
        return json.dumps(props, ensure_ascii=False, default=str, sort_keys=True)

    lines: list[tuple[str, str | None]] = []
    for n in ordered:
        lines.append((f"{n.id} {props_json(n.properties)}", n.id))
    for e in subgraph.edges:
        eline = f"{e.source} -[{e.type}]-> {e.target}"
        if e.properties:
            eline += f" {props_json(e.properties)}"
        lines.append((eline, None))

    budget = max(0, int(token_budget))
    parts: list[str] = []
    included: list[str] = []
    used = 0
    truncated = False
    for text, node_id in lines:
        cost = max(1, (len(text) + 1) // 4)
        if used + cost > budget:
            truncated = True
            continue
        parts.append(text)
        used += cost
        if node_id is not None:
            included.append(node_id)
    return PackedContext(
        text="\n".join(parts),
        token_estimate=used,
        included_node_ids=included,
        truncated=truncated,
    )
