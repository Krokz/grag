"""M06: complete evidence, bounded transport payloads, and resumable long text."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.errors import ConfigurationError, NotFoundError, SchemaError
from grag.core.serialize import estimate_tokens, pack_context
from grag.core.types import (
    VECTOR_PROPS,
    ContextRequest,
    EdgeRecord,
    FreshnessReport,
    NodeRecord,
    ScoredNode,
    SearchRequest,
    Subgraph,
)
from grag.mcp_server import server as mcp
from grag.retrieval.packing import (
    mcp_retrieval_text,
    pack_context_response,
    pack_search_response,
)
from grag.service import GragService


def _size(text: str) -> int:
    # Independent of production helper, including its rounding and Unicode.
    return (len(text.encode("utf-8")) + 3) // 4


def _footer(text: str) -> dict:
    return json.loads(text.rpartition("\n---\n")[2])


def _populate(service: GragService) -> None:
    engine = service.engine
    engine.execute_write(
        "CREATE NODE TABLE Doc(id STRING, title STRING, text STRING, "
        "_source STRING, primary key(id))"
    )
    engine.execute_write(
        "CREATE REL TABLE REFERENCES(FROM Doc TO Doc, note STRING, _source STRING)"
    )
    for i in range(4):
        engine.execute_write(
            "CREATE (:Doc {id: $id, title: $title, text: $text, _source: $source})",
            {
                "id": str(i),
                "title": f"orbital memory {i}",
                "text": "orbital "
                + ('日本語 אבג 🌍 \\"quoted\\"\n' * 150 if i == 0 else "details " * 30),
                "source": f"/workspace/notes/{i}.md",
            },
        )
        if i:
            engine.execute_write(
                "MATCH (a:Doc {id: '0'}), (b:Doc {id: $id}) "
                "CREATE (a)-[:REFERENCES {note: 'supports', _source: 'links.md'}]->(b)",
                {"id": str(i)},
            )


@pytest.fixture()
def server(tmp_path):
    server = mcp.create_server(
        GragConfig(db_path=tmp_path / "context.lbdb", embedder=None)
    )
    _populate(server.grag_service)
    try:
        yield server
    finally:
        server.grag_service.close()


@pytest.fixture()
def service(server):
    return server.grag_service


@pytest.mark.parametrize("budget", [256, 300, 512, 1000, 5000])
@pytest.mark.parametrize("mode", ["search", "context"])
def test_entire_response_is_bounded_and_values_never_silently_shortened(budget, mode):
    nodes = [
        NodeRecord(
            id=f"Doc:{i}",
            label="Doc",
            properties={
                "title": f"node {i}",
                "text": "decisive sentence " * 400,
                "nested": {"values": ["長い内容" * 30, 3, True]},
                "_source": "notes.md",
                **dict.fromkeys(VECTOR_PROPS, "vector" * 10_000),
            },
        )
        for i in range(8)
    ]
    edges = [
        EdgeRecord(
            id=f"R:{i}",
            type="R",
            source="Doc:0",
            target=f"Doc:{i}",
            properties={"note": "full relationship property " * 100},
        )
        for i in range(1, 8)
    ]
    original = Subgraph(nodes=nodes, edges=edges)
    before = original.model_dump_json()
    if mode == "search":
        response = pack_search_response(
            original,
            [
                ScoredNode(node=n, score=1 / (i + 1), match="fts")
                for i, n in enumerate(nodes)
            ],
            budget,
            pending_embeddings=12345,
            vector_status="error",
            index_status="refreshing",
        )
        returned = response.subgraph.node_map()
        assert all(seed.node == returned[seed.node.id] for seed in response.seeds)
    else:
        response = pack_context_response(original, budget, ["Doc:0"])
    text = mcp_retrieval_text(response)
    assert response.response_token_estimate == max(
        _size(response.model_dump_json()), _size(text)
    )
    assert response.response_token_estimate <= budget
    assert response.token_estimate == _size(response.context)
    assert response.truncated
    assert response.included_node_ids == [n.id for n in response.subgraph.nodes]
    assert response.omitted_nodes == len(nodes) - len(response.subgraph.nodes)
    assert response.omitted_edges == len(edges) - len(response.subgraph.edges)
    ids = set(response.included_node_ids)
    assert all({e.source, e.target} <= ids for e in response.subgraph.edges)
    original_props = {r.id: r.properties for r in [*nodes, *edges]}
    omitted_properties = 0
    for record in [*response.subgraph.nodes, *response.subgraph.edges]:
        assert not (record.properties.keys() & VECTOR_PROPS)
        expected = {
            k: v for k, v in original_props[record.id].items() if k not in VECTOR_PROPS
        }
        assert all(v == expected[k] for k, v in record.properties.items())
        omitted_properties += len(expected) - len(record.properties)
    assert response.omitted_properties == omitted_properties
    assert original.model_dump_json() == before  # packing never edits stored values


def test_original_audit_long_fact_is_available_with_room(service):
    decisive = "The fallback route is disabled when region affinity is enabled."
    body = "Background and history. " * 40 + decisive
    service.engine.execute_write(
        "MATCH (d:Doc {id:'0'}) SET d.text=$body", {"body": body}
    )
    response = service.get_context(
        ContextRequest(node_ids=["Doc:0"], hops=0, token_budget=5000)
    )
    assert decisive in response.context
    assert response.subgraph.nodes[0].properties["text"] == body
    assert not response.truncated
    assert response.omitted_properties == 0
    assert response.response_token_estimate < 5000


def test_whole_relationship_values_and_endpoints_survive_when_room(service):
    note = "reason " * 60 + "therefore keep the edge"
    service.engine.execute_write(
        "MATCH ()-[r:REFERENCES]->() SET r.note=$note", {"note": note}
    )
    response = service.get_context(
        ContextRequest(node_ids=["Doc:1"], token_budget=20_000)
    )
    assert not response.truncated
    assert note in response.context
    ids = set(response.included_node_ids)
    assert all({e.source, e.target} <= ids for e in response.subgraph.edges)
    assert all(e.properties["note"] == note for e in response.subgraph.edges)


def test_oversized_body_does_not_crowd_out_connecting_edges(service):
    response = service.get_context(
        ContextRequest(node_ids=["Doc:0"], token_budget=1000)
    )
    assert response.truncated and response.omitted_properties
    assert len(response.subgraph.nodes) >= 2
    assert response.subgraph.edges
    assert "REFERENCES" in response.context
    assert "..." not in response.context


@pytest.mark.parametrize("token_budget", [256, 512])
def test_pages_reconstruct_unicode_text_exactly_and_are_bounded(service, token_budget):
    expected = service.engine.execute("MATCH (d:Doc {id:'0'}) RETURN d.text").rows[0][0]
    offset = 0
    digest = None
    parts = []
    for _ in range(200):
        response = service.get_context(
            ContextRequest(
                node_ids=["Doc:0"],
                text_property="text",
                text_offset=offset,
                text_sha256=digest,
                token_budget=token_budget,
            )
        )
        page = response.text_page
        assert page is not None
        assert not response.subgraph.edges  # page mode deliberately skips expansion
        assert response.response_token_estimate == _size(response.model_dump_json())
        assert response.response_token_estimate <= token_budget
        assert page.total_chars == len(expected)
        assert page.sha256 == hashlib.sha256(expected.encode()).hexdigest()
        piece = response.subgraph.nodes[0].properties["text"]
        end = page.next_offset if page.next_offset is not None else len(expected)
        assert piece == expected[offset:end]
        assert piece and page.offset == offset
        assert (
            response.subgraph.nodes[0].properties["_source"] == "/workspace/notes/0.md"
        )
        assert response.truncated == (offset > 0 or end < len(expected))
        parts.append(piece)
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset, digest = page.next_offset, page.sha256
    else:
        pytest.fail("text paging did not finish")
    assert len(parts) > 1
    assert "".join(parts) == expected


def test_changed_text_refuses_continuation_and_restart_works(service):
    first = service.get_context(
        ContextRequest(
            node_ids=["Doc:0"],
            text_property="text",
            token_budget=256,
        )
    )
    page = first.text_page
    assert page is not None and page.next_offset is not None
    service.engine.execute_write("MATCH (d:Doc {id:'0'}) SET d.text='corrected memory'")
    with pytest.raises(NotFoundError, match="Text changed"):
        service.get_context(
            ContextRequest(
                node_ids=["Doc:0"],
                text_property="text",
                text_offset=page.next_offset,
                text_sha256=page.sha256,
            )
        )
    fresh = service.get_context(
        ContextRequest(node_ids=["Doc:0"], text_property="text")
    )
    assert fresh.subgraph.nodes[0].properties["text"] == "corrected memory"
    assert fresh.text_page.next_offset is None
    assert not fresh.truncated


@pytest.mark.parametrize("value", ["", "short 🌍 value"])
def test_empty_text_and_explicit_end_of_text(service, value):
    service.engine.execute_write(
        "MATCH (d:Doc {id:'0'}) SET d.text=$value", {"value": value}
    )
    response = service.get_context(
        ContextRequest(
            node_ids=["Doc:0"],
            text_property="text",
            text_offset=len(value),
        )
    )
    assert response.subgraph.nodes[0].properties["text"] == ""
    assert response.text_page.next_offset is None
    assert response.truncated == bool(value)


@pytest.mark.parametrize(
    "options, error",
    [
        ({"text_property": "absent"}, SchemaError),
        ({"text_property": "embedding"}, SchemaError),
        ({"text_property": "text", "text_offset": 1_000_000}, SchemaError),
        ({"text_property": "text", "node_ids": ["Doc:missing"]}, NotFoundError),
    ],
)
def test_invalid_text_page_is_actionable(service, options, error):
    with pytest.raises(error):
        service.get_context(ContextRequest(**{"node_ids": ["Doc:0"], **options}))


def test_null_is_not_a_string_page(service):
    service.engine.execute_write("MATCH (d:Doc {id:'0'}) SET d.text=NULL")
    with pytest.raises(SchemaError, match="non-text property"):
        service.get_context(ContextRequest(node_ids=["Doc:0"], text_property="text"))


def test_page_never_loops_without_progress_when_citation_cannot_fit(service):
    service.engine.execute_write(
        "MATCH (d:Doc {id:'0'}) SET d._source=$source",
        {"source": "long citation " * 500},
    )
    with pytest.raises(ConfigurationError, match="cannot fit a text page"):
        service.get_context(
            ContextRequest(
                node_ids=["Doc:0"],
                text_property="text",
                token_budget=256,
            )
        )


@pytest.mark.parametrize(
    "options",
    [
        {"text_property": ""},
        {"text_offset": 1},
        {"text_sha256": "unused"},
        {"text_property": "text", "text_offset": -1},
        {"text_property": "text", "node_ids": []},
        {"text_property": "text", "node_ids": ["Doc:0", "Doc:1"]},
    ],
)
def test_invalid_page_shapes_are_rejected(options):
    with pytest.raises(ValidationError):
        ContextRequest(**{"node_ids": ["Doc:0"], **options})


@pytest.mark.parametrize("budget", [-1, 0, 1, 255])
def test_tiny_budgets_are_rejected_instead_of_ignored(service, budget):
    for request, fields in [
        (SearchRequest, {"query": "orbital"}),
        (ContextRequest, {"node_ids": ["Doc:0"]}),
    ]:
        with pytest.raises(ValidationError, match="256"):
            request(**fields, token_budget=budget)
    assert "256" in mcp.search_knowledge(service, "orbital", token_budget=budget)
    assert mcp.get_context(service, ["Doc:0"], token_budget=budget).startswith("ERROR:")


def test_invalid_default_budget_is_reported_and_request_override_works(service):
    service.config.default_token_budget = 0
    with pytest.raises(ConfigurationError, match="256"):
        service.search_knowledge(SearchRequest(query="orbital"))
    response = service.get_context(ContextRequest(node_ids=["Doc:0"], token_budget=256))
    assert response.response_token_estimate <= 256


@pytest.mark.parametrize("limit, expected", [(2, True), (3, False)])
def test_expansion_cap_is_distinct_from_packing_omissions(
    service, monkeypatch, limit, expected
):
    monkeypatch.setattr("grag.retrieval.search._MAX_EXPANSION_PATHS", limit)
    for response in [
        service.get_context(ContextRequest(node_ids=["Doc:0"], token_budget=20_000)),
        service.search_knowledge(
            SearchRequest(query="orbital", top_k=4, token_budget=20_000)
        ),
    ]:
        assert response.expansion_limited == expected
        assert response.truncated == expected
        assert (
            response.omitted_nodes
            == response.omitted_edges
            == response.omitted_properties
            == 0
        )


def test_registered_mcp_tools_keep_metadata_pages_and_budget(server, monkeypatch):
    monkeypatch.setattr(server.grag_service, "read_freshness", lambda policy=None: FreshnessReport(status="refreshing"))
    calls = [
        ("search_knowledge", {"query": "orbital", "token_budget": 512}),
        ("get_context", {"node_ids": ["Doc:0"], "token_budget": 512}),
        (
            "get_context",
            {"node_ids": ["Doc:0"], "text_property": "text", "token_budget": 512},
        ),
    ]
    for name, args in calls:
        result = asyncio.run(server.call_tool(name, args))
        assert not result.is_error
        text = result.content[0].text
        assert not text.startswith("ERROR")
        footer = _footer(text)
        assert footer["truncated"] is True
        assert _size(text) <= footer["response_token_estimate"] <= 512
        assert footer["token_estimate"] == _size(
            text.rpartition("\n---\n")[0].rstrip("\n")
        )
        if name == "search_knowledge":
            assert footer["index"] == "refreshing"
            assert footer["vector"] == "off"
        if "text_property" in args:
            assert footer["text_page"]["next_offset"] > 0
    schema = next(
        t for t in asyncio.run(server.list_tools()) if t.name == "get_context"
    )
    assert {"text_property", "text_offset", "text_sha256"} <= schema.input_schema[
        "properties"
    ].keys()


@pytest.mark.parametrize(
    "endpoint, payload_in",
    [
        ("search", {"query": "orbital"}),
        ("context", {"node_ids": ["Doc:0"]}),
        ("context", {"node_ids": ["Doc:0"], "text_property": "text"}),
    ],
)
def test_rest_actual_json_payload_is_bounded(tmp_path, endpoint, payload_in):
    app = create_app(GragConfig(db_path=tmp_path / "rest.lbdb", embedder=None))
    with TestClient(app) as client:
        _populate(app.state.service)
        response = client.post(
            f"/api/{endpoint}", json={**payload_in, "token_budget": 512}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["truncated"]
        assert _size(response.text) == payload["response_token_estimate"] <= 512
        assert payload["token_estimate"] == _size(payload["context"])
        for bad in [0, -1, 255]:
            assert (
                client.post(
                    f"/api/{endpoint}", json={**payload_in, "token_budget": bad}
                ).status_code
                == 422
            )


def test_orphan_edge_is_omitted_and_reported():
    graph = Subgraph(
        nodes=[NodeRecord(id="Doc:0", label="Doc")],
        edges=[
            EdgeRecord(id="R:orphan", type="R", source="Doc:0", target="Doc:missing"),
        ],
    )
    response = pack_context_response(graph, 256, ["Doc:0"])
    assert response.truncated and response.omitted_edges == 1
    assert not response.subgraph.edges
    assert "missing" not in response.context


def test_unicode_rounding_and_multiline_sources():
    for value in ["a", "abc", "abcde", "שלום 日本語", "🌍", '"\\\n\t']:
        assert estimate_tokens(value) == _size(value)
    source = "notes]\n---\n[source:tail.md"
    response = pack_context(
        Subgraph(
            nodes=[
                NodeRecord(
                    id="Doc:0",
                    label="Doc",
                    properties={"_source": source, "text": "x" * 600},
                )
            ]
        ),
        5000,
    )
    assert len(response.text.splitlines()) == 1
    assert json.dumps(source, ensure_ascii=False) in response.text
    assert "x" * 600 in response.text
    assert response.subgraph.nodes[0].properties["_source"] == source


def test_oversized_identity_cannot_escape_budget():
    node = NodeRecord(
        id="Doc:" + "長" * 3000, label="Doc", properties={"text": "value"}
    )
    response = pack_search_response(
        Subgraph(nodes=[node]),
        [ScoredNode(node=node, score=1, match="fts")],
        256,
    )
    assert response.response_token_estimate <= 256
    assert response.truncated and response.omitted_nodes == 1
    assert not response.seeds and not response.subgraph.nodes


def test_long_seed_ids_leave_room_for_a_connecting_neighbor():
    nodes = [
        NodeRecord(
            id=f"Function:{i}:" + "x" * 100,
            label="Function",
            properties={"body": "long definition " * 1000},
        )
        for i in range(8)
    ]
    neighbor = NodeRecord(id="Module:" + "y" * 400, label="Module")
    edge = EdgeRecord(
        id=f"R:{nodes[0].id}->{neighbor.id}",
        type="R",
        source=nodes[0].id,
        target=neighbor.id,
    )
    response = pack_search_response(
        Subgraph(nodes=[*nodes, neighbor], edges=[edge]),
        [ScoredNode(node=n, score=1, match="fts") for n in nodes],
        2000,
    )
    assert response.included_node_ids[0] == nodes[0].id
    assert neighbor.id in response.included_node_ids
    assert response.subgraph.edges == [edge]
    assert response.response_token_estimate <= 2000
    assert response.truncated
