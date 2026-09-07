"""One budget and completeness policy for Python, REST, and MCP retrieval."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeVar

from grag.core.errors import ConfigurationError, NotFoundError, SchemaError
from grag.core.serialize import estimate_tokens, pack_context
from grag.core.types import (
    MIN_RETRIEVAL_BUDGET,
    PROVENANCE_SOURCE,
    VECTOR_PROPS,
    ContextRequest,
    ContextResponse,
    FreshnessReport,
    NodeRecord,
    PackedContext,
    ScoredNode,
    SearchResponse,
    Subgraph,
    TextPage,
)

Response = TypeVar("Response", SearchResponse, ContextResponse)


def retrieval_budget(requested: int | None, default: int) -> int:
    budget = default if requested is None else requested
    if budget < MIN_RETRIEVAL_BUDGET:
        raise ConfigurationError(
            f"Retrieval token_budget must be at least {MIN_RETRIEVAL_BUDGET}.",
            hint="Increase token_budget or GRAG_TOKEN_BUDGET to leave room for metadata.",
        )
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
) -> SearchResponse:
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
            )
        )

    packed = pack_context(
        subgraph,
        budget,
        [s.node.id for s in seeds],
        measure=lambda p: response(p).response_token_estimate,
    )
    return response(packed)


def pack_context_response(
    subgraph: Subgraph,
    budget: int,
    seed_ids: list[str],
    *,
    expansion_limited: bool = False,
    freshness: FreshnessReport | None = None,
) -> ContextResponse:
    def response(packed: PackedContext) -> ContextResponse:
        return measure_response(ContextResponse(**_fields(packed, expansion_limited, freshness)))

    packed = pack_context(
        subgraph,
        budget,
        seed_ids,
        measure=lambda p: response(p).response_token_estimate,
    )
    return response(packed)


def pack_text_page(
    node: NodeRecord, req: ContextRequest, budget: int, *, freshness: FreshnessReport | None = None,
) -> ContextResponse:
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
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if req.text_sha256 is not None and digest != req.text_sha256:
        raise NotFoundError(
            "Text changed since the previous page.",
            hint="Restart with text_offset=0 and omit text_sha256.",
        )
    if req.text_offset > len(value):
        raise SchemaError(
            "text_offset is beyond the end of the STRING.",
            hint=f"Use a character offset between 0 and {len(value)}.",
        )

    def response(end: int) -> ContextResponse:
        props = {prop: value[req.text_offset : end]}
        if prop != PROVENANCE_SOURCE and node.properties.get(PROVENANCE_SOURCE):
            props[PROVENANCE_SOURCE] = node.properties[PROVENANCE_SOURCE]
        projected = node.model_copy(update={"properties": props})
        packed = pack_context(Subgraph(nodes=[projected]), token_budget=2**63 - 1)
        return measure_response(
            ContextResponse(
                **{
                    **_fields(packed, False, freshness),
                    "truncated": req.text_offset > 0 or end < len(value),
                },
                text_page=TextPage(
                    node_id=node.id,
                    property=prop,
                    offset=req.text_offset,
                    next_offset=end if end < len(value) else None,
                    total_chars=len(value),
                    sha256=digest,
                ),
            )
        )

    full = response(len(value))
    if full.response_token_estimate <= budget:
        return full
    low, high = req.text_offset, len(value) - 1
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
