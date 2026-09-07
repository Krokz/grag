"""One budget and completeness policy for Python, REST, and MCP retrieval."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections import deque
from typing import Literal, TypeVar

from grag.core.errors import ConfigurationError, NotFoundError, SchemaError
from grag.core.limits import MAX_TOKEN_BUDGET, charge, check_size
from grag.core.serialize import estimate_tokens, pack_context
from grag.core.types import (
    MIN_RETRIEVAL_BUDGET,
    PROVENANCE_SOURCE,
    VECTOR_PROPS,
    ContextRequest,
    ContextResponse,
    EvidenceMode,
    FreshnessReport,
    NodeRecord,
    PackedContext,
    ScoredNode,
    SearchResponse,
    Subgraph,
    TextExcerpt,
    TextPage,
)

Response = TypeVar("Response", SearchResponse, ContextResponse)


def _qualify(subgraph: Subgraph) -> Subgraph:
    from grag.core.evidence import exclusion_reason

    now = dt.datetime.now(dt.timezone.utc)
    nodes = []
    for node in subgraph.nodes:
        reason = exclusion_reason(node, now)
        nodes.append(node.model_copy(update={"properties": {**node.properties, "_evidence_visibility": reason}}) if reason else node)
    return Subgraph(nodes=nodes, edges=subgraph.edges)

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
_QUERY_FILLER = frozenset(["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "should", "that", "the", "this", "to", "was", "what", "when", "where", "which", "who", "why", "will", "with"])


def _terms(text: str) -> set[str]:
    charge("lexical_bytes", len(text.encode("utf-8")))
    tokens = _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
    charge("lexical_terms", len(tokens))
    return set(tokens) - _QUERY_FILLER


def _sentences(text: str):
    start = 0
    for match in _SENTENCE_END.finditer(text):
        yield start, match.end()
        start = match.end()
    if start < len(text):
        yield start, len(text)


def _order_expansion(subgraph: Subgraph, seeds: list[ScoredNode], query: str) -> Subgraph:
    """Keep seeds fixed; prioritize nearby evidence and connecting neighbors.

    This orders the already retrieved graph, not the search shortlist. Zero
    lexical overlap never removes a node or edge: semantic-only connections
    remain candidates, and packing reports anything it cannot include.
    """
    if not query.strip() or not seeds:
        return subgraph
    seed_ids = {s.node.id for s in seeds}
    adjacent: dict[str, set[str]] = {}
    for edge in subgraph.edges:
        adjacent.setdefault(edge.source, set()).add(edge.target)
        adjacent.setdefault(edge.target, set()).add(edge.source)
    distance = dict.fromkeys(seed_ids, 0)
    queue = deque(sorted(seed_ids))
    while queue:
        nid = queue.popleft()
        for neighbor in sorted(adjacent.get(nid, set())):
            if neighbor not in distance:
                distance[neighbor] = distance[nid] + 1
                queue.append(neighbor)
    terms = _terms(query)

    def priority(node: NodeRecord):
        evidence = "\n".join(v for k, v in node.properties.items()
                             if not k.startswith("_") and k not in {"id", "path", "meta"}
                             and k not in VECTOR_PROPS and isinstance(v, str))
        connects = len(adjacent.get(node.id, set()) & seed_ids)
        return (-int(connects >= 2), distance.get(node.id, len(subgraph.nodes)),
                -len(terms & _terms(evidence)), node.id)

    nodes = sorted(subgraph.nodes, key=priority)
    order = {n.id: i for i, n in enumerate([*(s.node for s in seeds), *(n for n in nodes if n.id not in seed_ids)])}
    edges = sorted(subgraph.edges, key=lambda e: (
        max(order.get(e.source, len(order)), order.get(e.target, len(order))), e.id,
    ))
    return Subgraph(nodes=nodes, edges=edges)


def _text_excerpts(subgraph: Subgraph, query: str) -> list[TextExcerpt]:
    """Offer exact sentence windows; packing decides whether they fit.

    No generation, prefix clipping or fabricated property values. A window
    includes the adjacent sentences when feasible, with a single-sentence
    fallback. Selection is lexical and may miss semantic-only matches.
    """
    terms = _terms(query)
    if not terms:
        return []
    excerpts = []
    for node in subgraph.nodes:
        for prop, value in node.properties.items():
            if prop.startswith("_") or prop in VECTOR_PROPS or not isinstance(value, str) or len(value) < 512:
                continue
            # A long identifier or path is not prose to excerpt.
            if prop in {"id", "path", "name", "title", "signature", "meta", "heading_path"}:
                continue
            # Stream sentence spans: even a very long value needs only the
            # previous/current/next window in memory, not a list of all spans.
            iterator = _sentences(value)
            previous = None
            current = next(iterator, None)
            best = None
            best_score = 0
            while current is not None:
                following = next(iterator, None)
                start, end = current
                score = len(terms & _terms(value[start:end])) if end - start <= 1200 else 0
                if score > best_score:
                    best_score = score
                    best = ((previous[0] if previous else start, following[1] if following else end), current)
                previous, current = current, following
            if best is None:
                continue
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            spans = list(best)
            seen = set()
            for window_start, window_end in spans:
                start, end = window_start, window_end
                while start < end and value[start].isspace():
                    start += 1
                while end > start and value[end - 1].isspace():
                    end -= 1
                if end - start > 1200 or start == end or (start, end) in seen or (start == 0 and end == len(value)):
                    continue
                seen.add((start, end))
                excerpts.append(TextExcerpt(node_id=node.id, property=prop, text=value[start:end],
                                            offset=start, end=end, total_chars=len(value), sha256=digest))
    return excerpts


def retrieval_budget(requested: int | None, default: int) -> int:
    budget = default if requested is None else requested
    if budget < MIN_RETRIEVAL_BUDGET:
        raise ConfigurationError(
            f"Retrieval token_budget must be at least {MIN_RETRIEVAL_BUDGET}.",
            hint="Increase token_budget or GRAG_TOKEN_BUDGET to leave room for metadata.",
        )
    check_size("token_budget", budget, MAX_TOKEN_BUDGET)
    return budget


def mcp_retrieval_text(resp: SearchResponse | ContextResponse) -> str:
    """The actual MCP tool text, also measured before the response is accepted."""
    payload = {
        key: getattr(resp, key)
        for key in (
            "token_estimate",
            "response_token_estimate",
            "included_node_ids",
            "truncated",
            "omitted_nodes",
            "omitted_edges",
            "omitted_properties",
            "expansion_limited",
        )
    }
    payload["freshness"] = resp.freshness.model_dump()
    if resp.evidence_policy is not None:
        payload["evidence_policy"] = resp.evidence_policy
        payload["excluded_evidence"] = resp.excluded_evidence
    if isinstance(resp, ContextResponse) and resp.history is not None:
        payload["history"] = resp.history.model_dump()
    if resp.text_excerpts:
        # Exact text is already rendered in context; retain the follow-up
        # coordinates and hash without duplicating that text in the footer.
        payload["text_excerpts"] = [e.model_dump(exclude={"text"}) for e in resp.text_excerpts]
    if isinstance(resp, SearchResponse):
        payload["seeds"] = [
            {"id": s.node.id, "score": round(s.score, 6), "match": s.match}
            for s in resp.seeds
        ]
        if resp.pending_embeddings:
            payload["pending_embeddings"] = resp.pending_embeddings
        if resp.vector_status:
            payload["vector"] = resp.vector_status
        if resp.index_status:
            payload["index"] = resp.index_status
    elif resp.text_page is not None:
        payload["text_page"] = resp.text_page.model_dump()
    footer = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{resp.context}\n\n---\n{footer}" if resp.context else footer


def measure_response(resp: Response) -> Response:
    # The estimate counts its own digits; converge before testing the budget.
    resp.response_token_estimate = 0
    while True:
        measured = max(
            estimate_tokens(resp.model_dump_json()),
            estimate_tokens(mcp_retrieval_text(resp)),
        )
        if measured == resp.response_token_estimate:
            return resp
        resp.response_token_estimate = measured


def _fields(
    packed: PackedContext, expansion_limited: bool, freshness: FreshnessReport | None = None,
) -> dict:
    return {
        "context": packed.text,
        "token_estimate": packed.token_estimate,
        "included_node_ids": packed.included_node_ids,
        "truncated": packed.truncated or expansion_limited,
        "omitted_nodes": packed.omitted_nodes,
        "omitted_edges": packed.omitted_edges,
        "omitted_properties": packed.omitted_properties,
        "expansion_limited": expansion_limited,
        "subgraph": packed.subgraph,
        "freshness": freshness or FreshnessReport(),
        "text_excerpts": packed.text_excerpts,
    }


def pack_search_response(
    subgraph: Subgraph,
    seeds: list[ScoredNode],
    budget: int,
    *,
    pending_embeddings: int = 0,
    vector_status: Literal["off", "error"] | None = None,
    index_status: Literal["refreshing"] | None = None,
    expansion_limited: bool = False,
    freshness: FreshnessReport | None = None,
    query: str = "",
    evidence_policy: EvidenceMode | None = None,
    excluded_evidence: int = 0,
) -> SearchResponse:
    if evidence_policy == "all":
        subgraph = _qualify(subgraph)
    subgraph = _order_expansion(subgraph, seeds, query)

    def response(packed: PackedContext) -> SearchResponse:
        nodes = packed.subgraph.node_map()
        return measure_response(
            SearchResponse(
                **_fields(packed, expansion_limited, freshness),
                seeds=[
                    s.model_copy(update={"node": nodes[s.node.id]})
                    for s in seeds
                    if s.node.id in nodes
                ],
                pending_embeddings=pending_embeddings,
                vector_status=vector_status,
                index_status=index_status,
                evidence_policy=evidence_policy,
                excluded_evidence=excluded_evidence,
            )
        )

    packed = pack_context(
        subgraph,
        budget,
        [s.node.id for s in seeds],
        measure=lambda p: response(p).response_token_estimate,
        excerpts=_text_excerpts(subgraph, query),
    )
    return response(packed)


def pack_context_response(
    subgraph: Subgraph,
    budget: int,
    seed_ids: list[str],
    *,
    expansion_limited: bool = False,
    freshness: FreshnessReport | None = None,
    evidence_policy: EvidenceMode | None = None,
    excluded_evidence: int = 0,
) -> ContextResponse:
    if evidence_policy == "all":
        subgraph = _qualify(subgraph)
    def response(packed: PackedContext) -> ContextResponse:
        return measure_response(ContextResponse(**_fields(packed, expansion_limited, freshness),
                                               evidence_policy=evidence_policy, excluded_evidence=excluded_evidence))

    packed = pack_context(
        subgraph,
        budget,
        seed_ids,
        measure=lambda p: response(p).response_token_estimate,
    )
    return response(packed)


def pack_text_page(
    node: NodeRecord, req: ContextRequest, budget: int, *, freshness: FreshnessReport | None = None,
    page_origin: int = 0, page_total: int | None = None, page_digest: str | None = None,
) -> ContextResponse:
    if req.evidence == "all" or req.revision is not None:
        node = _qualify(Subgraph(nodes=[node])).nodes[0]
    prop = req.text_property
    if prop is None:
        raise SchemaError("Text paging requires text_property.")
    if prop in VECTOR_PROPS or prop not in node.properties:
        raise SchemaError(
            f"Unknown or non-text property {prop!r} on {node.id}.",
            hint="Use describe_schema to choose a STRING property.",
        )
    value = node.properties[prop]
    if not isinstance(value, str):
        raise SchemaError(f"Property {prop!r} on {node.id} is not a STRING value.")
    digest = page_digest or hashlib.sha256(value.encode("utf-8")).hexdigest()
    total = len(value) if page_total is None else page_total
    if req.text_sha256 is not None and digest != req.text_sha256:
        raise NotFoundError(
            "Text changed since the previous page.",
            hint="Restart with text_offset=0 and omit text_sha256.",
        )
    if req.text_offset > total:
        raise SchemaError(
            "text_offset is beyond the end of the STRING.",
            hint=f"Use a character offset between 0 and {len(value)}.",
        )

    def response(end: int) -> ContextResponse:
        from grag.core.evidence import SAFETY_PROPS

        props = {prop: value[req.text_offset - page_origin : end - page_origin]}
        props.update({k: v for k, v in node.properties.items() if k != prop and k in SAFETY_PROPS})
        if prop != PROVENANCE_SOURCE and node.properties.get(PROVENANCE_SOURCE):
            props[PROVENANCE_SOURCE] = node.properties[PROVENANCE_SOURCE]
        projected = node.model_copy(update={"properties": props})
        packed = pack_context(Subgraph(nodes=[projected]), token_budget=2**63 - 1)
        return measure_response(
            ContextResponse(
                **{
                    **_fields(packed, False, freshness),
                    "truncated": req.text_offset > 0 or end < total,
                },
                text_page=TextPage(
                    node_id=node.id,
                    property=prop,
                    offset=req.text_offset,
                    next_offset=end if end < total else None,
                    total_chars=total,
                    sha256=digest,
                ),
                evidence_policy="all" if req.revision is not None else req.evidence,
            )
        )

    # One estimated token can cover at most four UTF-8 bytes here. Never
    # serialize an entire large property just to discover it cannot fit.
    end_limit = min(page_origin + len(value), req.text_offset + 4 * budget)
    full = response(end_limit)
    if full.response_token_estimate <= budget:
        return full
    low, high = req.text_offset, end_limit - 1
    best: ContextResponse | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = response(middle)
        if candidate.response_token_estimate <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if (
        best is None
        or best.text_page is None
        or best.text_page.next_offset == req.text_offset
    ):
        raise ConfigurationError(
            "token_budget cannot fit a text page with its id, citation, and metadata.",
            hint="Increase token_budget; a page must return at least one character.",
        )
    return best
