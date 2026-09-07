"""Hybrid retrieval: FTS + vector seeds, RRF fusion, k-hop expansion, and
token-budgeted context packing.

FTS indexes (one per searchable node table, named fts_index_name(table)) are
created lazily on first query and auto-maintained by LadybugDB afterwards.
Vector seeds are best-effort: any failure in the vector path degrades the
search to FTS-only rather than failing the request.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import weakref
from typing import Any, Literal

from grag.config import GragConfig
from grag.core.engine import Engine, extract_subgraph, node_record_from_value
from grag.core.errors import GragError, ResourceLimitError
from grag.core.evidence import current_predicate, exclusion_reason
from grag.core.limits import MAX_EXPANSION_PATHS, bounded_work, candidate_quota
from grag.core.types import (
    FreshnessReport,
    ScoredNode,
    SearchRequest,
    SearchResponse,
    Subgraph,
    fts_index_name,
    merge_subgraphs,
)
from grag.retrieval.lexical import rank_lexical
from grag.retrieval.packing import pack_search_response, retrieval_budget
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


@bounded_work
def search_knowledge(
    engine: Engine,
    config: GragConfig,
    req: SearchRequest,
    *,
    index_status: Literal["refreshing"] | None = None,
    freshness: FreshnessReport | None = None,
) -> SearchResponse:
    top_k = max(1, req.top_k)
    now = dt.datetime.now(dt.timezone.utc)
    excluded: set[str] = set()

    def eligible(scored: ScoredNode) -> bool:
        if req.evidence == "current" and exclusion_reason(scored.node, now):
            excluded.add(scored.node.id)
            return False
        return True
    hops = max(0, min(req.hops, config.max_hops))
    budget = retrieval_budget(req.token_budget, config.default_token_budget)
    pk = pk_map_with_fallback(engine)
    tables = candidate_tables(engine, config, req.labels)
    # Oversample each label so fusion/diversity can select beyond a modality's
    # first top_k. A bounded shortlist is not an exhaustive graph-wide ranking.
    candidate_k = candidate_quota(tables, max(32, 4 * top_k))

    fts_list: list[ScoredNode] = []
    vec_list: list[ScoredNode] = []
    pending = 0
    vector_status: Literal["off", "error"] | None = None
    if req.query.strip():
        text_properties = {table: string_props(engine, table) for table in tables}
        for table in tables:
            fts_list.extend(
                _fts_seeds(
                    engine,
                    table,
                    req.query,
                    candidate_k,
                    pk,
                    cols=text_properties[table],
                    evidence_now=now if req.evidence == "current" else None,
                )
            )
        fts_list = rank_lexical(engine, [s for s in fts_list if eligible(s)], req.query, text_properties)
        if config.embedder is None:
            vector_status = "off"
        else:
            try:
                vec_list = vector_candidates(
                    engine, config, req.query, tables, candidate_k, per_table=True,
                    evidence_now=now if req.evidence == "current" else None,
                )
                vec_list = [s for s in vec_list if eligible(s)]
                pending = sum(
                    pending_embedding_count(engine, config, t) for t in tables
                )
            except ResourceLimitError:
                raise  # never conceal exhausted work as ordinary FTS-only retrieval
            except Exception as exc:  # noqa: BLE001 — vector path is best-effort
                log.warning("Vector search skipped, degrading to FTS-only: %s", exc)
                vector_status = "error"

    fused = _rrf_fuse({"fts": fts_list, "vector": vec_list})
    seeds = _diversify(fused, top_k, config.search_label_cap)
    expanded, expansion_limited = _expand_neighborhood(
        engine, _seed_refs(seeds, pk), hops, pk,
        excluded=excluded if req.evidence == "current" else None, now=now,
    )
    subgraph = merge_subgraphs(Subgraph(nodes=[s.node for s in seeds]), expanded)
    return pack_search_response(
        subgraph,
        seeds,
        budget,
        pending_embeddings=pending,
        vector_status=vector_status,
        index_status=index_status,
        expansion_limited=expansion_limited,
        freshness=freshness,
        query=req.query,
        evidence_policy=req.evidence,
        excluded_evidence=len(excluded),
    )


# ---------------------------------------------------------------------------
# FTS seeds
# ---------------------------------------------------------------------------


_FTS_INDEXES: weakref.WeakKeyDictionary[Engine, set[str]] = weakref.WeakKeyDictionary()


def _fts_seeds(
    engine: Engine,
    table: str,
    query: str,
    top_k: int,
    pk: dict[str, str],
    *,
    cols: list[str] | None = None,
    evidence_now: dt.datetime | None = None,
) -> list[ScoredNode]:
    if cols is None:
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
    params = {"q": query}
    predicate = current_predicate(engine, table, "node") if evidence_now is not None else "true"
    if predicate != "true" and evidence_now is not None:
        # Native TOP runs before WHERE. Filter first so a table's retired
        # memories cannot monopolize the bounded shortlist.
        cypher = (f"CALL QUERY_FTS_INDEX('{_ident(table)}', '{index}', $q) "
                  f"WITH node, score WHERE {predicate} RETURN node, score "
                  f"ORDER BY score DESC, node.{_ident(pk[table])} LIMIT {int(top_k)}")
        params["evidence_now"] = evidence_now.isoformat()
    try:
        res = engine.execute(cypher, params)
    except ResourceLimitError:
        raise
    except GragError:
        # reader may hold a stale catalog; the write connection is authoritative
        res = engine.execute_write(cypher, params)
    # TOP bounds the set, but the native API does not promise result-row order.
    return sorted(
        [
            ScoredNode(
                node=node_record_from_value(nv, pk), score=float(score), match="fts"
            )
            for nv, score in res.rows
        ],
        key=lambda s: (-s.score, s.node.id),
    )


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
    """Fuse score-ordered modalities with competition ranks for equal scores.

    Duplicate hits contribute once per modality. Node IDs break final ties,
    never dict, table, query-label or native result-row order. The match label
    comes from the better modality rank; 'fts' wins ties.
    """
    ranks: dict[str, dict[str, int]] = {}
    nodes: dict[str, Any] = {}
    for source, lst in sorted(lists.items()):
        unique: dict[str, ScoredNode] = {}
        for scored in lst:
            if not math.isfinite(scored.score):
                continue
            old = unique.get(scored.node.id)
            if old is None or scored.score > old.score:
                unique[scored.node.id] = scored
        previous: float | None = None
        rank = 0
        for i, scored in enumerate(
            sorted(unique.values(), key=lambda s: (-s.score, s.node.id))
        ):
            if scored.score != previous:
                rank = i + 1
            previous = scored.score
            nid = scored.node.id
            nodes.setdefault(nid, scored.node)
            ranks.setdefault(nid, {})[source] = rank
    fused = []
    for nid, src_ranks in ranks.items():
        score = sum(1.0 / (_RRF_K + r) for r in src_ranks.values())
        fts_r = src_ranks.get("fts")
        vec_r = src_ranks.get("vector")
        match: Literal["fts", "vector"] = (
            "fts"
            if (fts_r is not None and (vec_r is None or fts_r <= vec_r))
            else "vector"
        )
        fused.append(ScoredNode(node=nodes[nid], score=score, match=match))
    fused.sort(key=lambda s: (-s.score, s.node.id))
    return fused


def _diversify(fused: list[ScoredNode], top_k: int, cap: int) -> list[ScoredNode]:
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


def _seed_refs(seeds: list[ScoredNode], pk: dict[str, str]) -> list[tuple[str, Any]]:
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
    engine: Engine, seed_refs: list[tuple[str, Any]], hops: int, pk: dict[str, str],
    *, excluded: set[str] | None = None, now: dt.datetime | None = None,
) -> tuple[Subgraph, bool]:
    """Undirected k-hop neighborhood of the seed nodes, across all rel types."""
    if hops <= 0 or not seed_refs:
        return Subgraph(), False
    subs = []
    limited = False
    remaining = MAX_EXPANSION_PATHS
    for label, key in seed_refs:
        if remaining == 0:
            limited = True
            break
        cap = min(_MAX_EXPANSION_PATHS, remaining)
        p = pk.get(label)
        if not p:
            continue
        res = engine.execute(
            f"MATCH p = (a:{_ident(label)} {{{_ident(p)}: $key}})-[*1..{int(hops)}]-(b) "
            f"RETURN p LIMIT {cap + 1}",
            {"key": key},
        )
        if len(res.rows) > cap:
            limited = True
            res.rows = res.rows[:cap]
        remaining -= len(res.rows)
        if excluded is None:
            subs.append(extract_subgraph(res, pk))
        else:
            # Reject the complete path: inactive evidence must not become a
            # hidden bridge to apparently connected current facts.
            for row in res.rows:
                path = extract_subgraph(type(res)(columns=res.columns, rows=[row]), pk)
                hidden = {n.id for n in path.nodes if exclusion_reason(n, now or dt.datetime.now(dt.timezone.utc))}
                excluded.update(hidden)
                if not hidden:
                    subs.append(path)
    return (merge_subgraphs(*subs) if subs else Subgraph()), limited
