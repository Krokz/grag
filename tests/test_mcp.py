"""Tests for grag.mcp_server.server — plain tool functions (full LLM workflow
simulation, error contract) plus FastMCP/MCPServer registration of the 7
frozen tool names."""

from __future__ import annotations

import asyncio
import json

import pytest

from grag.config import GragConfig
from grag.mcp_server import server as mcp_server
from grag.service import GragService

FROZEN_TOOLS = {
    "describe_schema",
    "define_schema",
    "upsert_nodes",
    "upsert_edges",
    "cypher_query",
    "search_knowledge",
    "get_context",
}

NODE_TABLES = [
    {
        "name": "Person",
        "primary_key": "name",
        "properties": [{"name": "age", "type": "INT64"}],
    },
    {
        "name": "Doc",
        "primary_key": "id",
        "properties": [{"name": "title"}, {"name": "body"}],
    },
]
REL_TABLES = [
    {
        "name": "KNOWS",
        "from_label": "Person",
        "to_label": "Person",
        "properties": [{"name": "since", "type": "INT64"}],
    },
    {"name": "AUTHORED", "from_label": "Person", "to_label": "Doc"},
]

PERSONS = [
    {"label": "Person", "key": "ada", "properties": {"age": 36}, "source": "people.md"},
    {"label": "Person", "key": "grace", "properties": {"age": 85}, "source": "people.md"},
    {"label": "Person", "key": "edsger", "properties": {"age": 72}},
]
DOCS = [
    {
        "label": "Doc",
        "key": "doc-1",
        "properties": {
            "title": "Graph databases",
            "body": "Graph databases store knowledge as nodes and relationships.",
        },
        "source": "graph.md",
    },
]
EDGES = [
    {
        "type": "KNOWS",
        "from_label": "Person",
        "from_key": "ada",
        "to_label": "Person",
        "to_key": "grace",
        "properties": {"since": 1835},
        "source": "people.md",
    },
    {
        "type": "AUTHORED",
        "from_label": "Person",
        "from_key": "ada",
        "to_label": "Doc",
        "to_key": "doc-1",
        "source": "graph.md",
    },
]


@pytest.fixture()
def service(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "mcp.lbdb"))
    yield svc
    svc.close()


def _define(service: GragService) -> None:
    out = mcp_server.define_schema(service, NODE_TABLES, REL_TABLES)
    assert not out.startswith("ERROR"), out


def _populate(service: GragService) -> None:
    _define(service)
    out = mcp_server.upsert_nodes(service, PERSONS + DOCS)
    assert not out.startswith("ERROR"), out
    out = mcp_server.upsert_edges(service, EDGES)
    assert not out.startswith("ERROR"), out


def _seed_ids(search_out: str) -> list[str]:
    _, _, footer = search_out.partition("\n---\n")
    payload = json.loads(footer if footer else search_out)
    return [s["id"] for s in payload["seeds"]]


# --- full LLM workflow simulation -------------------------------------------------


def test_full_llm_workflow(service: GragService):
    # 1. describe_schema on an empty db: valid str, no tables, no error
    empty = mcp_server.describe_schema(service)
    assert isinstance(empty, str)
    assert not empty.startswith("ERROR")
    assert "Person" not in empty

    # 2. define_schema returns the fresh schema text
    defined = mcp_server.define_schema(service, NODE_TABLES, REL_TABLES)
    assert not defined.startswith("ERROR")
    for name in ("Person", "Doc", "KNOWS", "AUTHORED"):
        assert name in defined

    # 3. describe_schema again reflects the new tables
    described = mcp_server.describe_schema(service)
    for name in ("Person", "Doc", "KNOWS", "AUTHORED"):
        assert name in described

    # 4. upserts report counts as compact JSON
    nodes_out = json.loads(mcp_server.upsert_nodes(service, PERSONS + DOCS))
    assert nodes_out == {"nodes": 4, "edges": 0, "warnings": []}
    edges_out = json.loads(mcp_server.upsert_edges(service, EDGES))
    assert edges_out == {"nodes": 0, "edges": 2, "warnings": []}

    # 5. cypher_query reads back through the graph
    result = json.loads(
        mcp_server.cypher_query(
            service, "MATCH (p:Person)-[:AUTHORED]->(d:Doc) RETURN p.name, d.title"
        )
    )
    assert result["columns"] == ["p.name", "d.title"]
    assert result["rows"] == [["ada", "Graph databases"]]
    assert result["row_count"] == 1
    assert result["truncated"] is False

    # 6. search_knowledge: context for grounding + seed ids for follow-up
    search_out = mcp_server.search_knowledge(service, "graph databases")
    assert not search_out.startswith("ERROR")
    context, sep, footer = search_out.partition("\n---\n")
    assert sep, "expected context + JSON seed footer"
    assert "Doc:doc-1" in context
    seeds = json.loads(footer)["seeds"]
    assert seeds and "Doc:doc-1" in [s["id"] for s in seeds]
    assert all(isinstance(s["score"], float) for s in seeds)

    # 7. get_context roundtrip on the seed ids
    ctx = mcp_server.get_context(service, [s["id"] for s in seeds], hops=1)
    assert not ctx.startswith("ERROR")
    assert "Doc:doc-1" in ctx
    assert "Person:ada" in ctx  # 1-hop expansion via AUTHORED
    assert "AUTHORED" in ctx


# --- cypher_query -------------------------------------------------------------------


def test_cypher_query_limit_and_truncation(service: GragService):
    _populate(service)
    out = json.loads(
        mcp_server.cypher_query(
            service, "MATCH (p:Person) RETURN p.name ORDER BY p.name", limit=2
        )
    )
    assert out["rows"] == [["ada"], ["edsger"]]
    assert out["row_count"] == 2
    assert out["truncated"] is True

    full = json.loads(mcp_server.cypher_query(service, "MATCH (p:Person) RETURN p.name"))
    assert full["row_count"] == 3
    assert full["truncated"] is False


def test_cypher_query_rejects_write_keywords(service: GragService):
    _define(service)
    out = mcp_server.cypher_query(service, "CREATE (p:Person {name: 'mallory'})")
    assert out.startswith("ERROR:")
    assert "read-only" in out
    assert "HINT:" in out and "upsert" in out
    # guardrail fired before anything was written
    check = json.loads(mcp_server.cypher_query(service, "MATCH (p:Person) RETURN count(p)"))
    assert check["rows"] == [[0]]


def test_cypher_query_bad_cypher_returns_error_with_hint(service: GragService):
    _define(service)
    out = mcp_server.cypher_query(service, "MATCH (p:Person RETURN p")
    assert out.startswith("ERROR:")
    assert "HINT:" in out
    assert "describe_schema" in out


# --- upserts -------------------------------------------------------------------------


def test_upsert_is_idempotent_and_warns_on_unknown_props(service: GragService):
    _define(service)
    first = json.loads(
        mcp_server.upsert_nodes(
            service, [{"label": "Person", "key": "ada", "properties": {"age": 36}}]
        )
    )
    assert first["nodes"] == 1 and first["warnings"] == []
    second = json.loads(
        mcp_server.upsert_nodes(
            service,
            [
                {
                    "label": "Person",
                    "key": "ada",
                    "properties": {"age": 37, "nickname": "countess"},
                }
            ],
        )
    )
    assert second["nodes"] == 1
    assert any("nickname" in w for w in second["warnings"])
    rows = json.loads(mcp_server.cypher_query(service, "MATCH (p:Person) RETURN p.age"))["rows"]
    assert rows == [[37]]


def test_upsert_nodes_unknown_label_returns_grag_error(service: GragService):
    out = mcp_server.upsert_nodes(service, [{"label": "Ghost", "key": 1}])
    assert out.startswith("ERROR:")
    assert "Ghost" in out
    assert "HINT:" in out and "define_schema" in out


def test_upsert_edges_missing_endpoint_returns_grag_error(service: GragService):
    _populate(service)
    out = mcp_server.upsert_edges(
        service,
        [
            {
                "type": "KNOWS",
                "from_label": "Person",
                "from_key": "ada",
                "to_label": "Person",
                "to_key": "nobody",
            }
        ],
    )
    assert out.startswith("ERROR:")
    assert "nobody" in out
    assert "HINT:" in out


# --- validation error contract -------------------------------------------------------


def test_upsert_nodes_malformed_dict_returns_error_string(service: GragService):
    _define(service)
    out = mcp_server.upsert_nodes(service, [{"label": "Person"}])  # missing "key"
    assert isinstance(out, str)  # returned, not raised
    assert out.startswith("ERROR:")
    assert "key" in out
    assert "HINT:" in out


def test_define_schema_malformed_spec_returns_error_string(service: GragService):
    out = mcp_server.define_schema(service, [{"properties": []}], [])  # missing "name"
    assert isinstance(out, str)
    assert out.startswith("ERROR:")
    assert "name" in out
    assert "HINT:" in out


# --- retrieval error paths ------------------------------------------------------------


def test_get_context_invalid_node_id_returns_error(service: GragService):
    _define(service)
    out = mcp_server.get_context(service, ["not-a-valid-id"])
    assert out.startswith("ERROR:")
    assert "HINT:" in out


def test_get_context_unknown_label_returns_error(service: GragService):
    _define(service)
    out = mcp_server.get_context(service, ["Ghost:1"])
    assert out.startswith("ERROR:")
    assert "Ghost" in out
    assert "HINT:" in out


def test_search_knowledge_empty_db_returns_empty_seeds(service: GragService):
    out = mcp_server.search_knowledge(service, "anything")
    assert not out.startswith("ERROR")
    payload = json.loads(out)  # no context, just the footer
    assert payload == {"seeds": []}


# --- MCP registration ------------------------------------------------------------------


def test_mcp_server_registers_all_frozen_tools(tmp_path):
    server = mcp_server.create_server(GragConfig(db_path=tmp_path / "app.lbdb"))
    try:
        tools = asyncio.run(server.list_tools())
        assert {t.name for t in tools} == FROZEN_TOOLS
        for t in tools:
            assert t.description, f"{t.name} lost its docstring"
            assert t.input_schema["type"] == "object"
    finally:
        server.grag_service.close()


def test_mcp_end_to_end_tool_calls(tmp_path):
    server = mcp_server.create_server(GragConfig(db_path=tmp_path / "e2e.lbdb"))
    try:
        res = asyncio.run(
            server.call_tool(
                "define_schema", {"node_tables": [NODE_TABLES[0]], "rel_tables": []}
            )
        )
        assert not res.is_error
        assert "Person" in res.content[0].text

        res = asyncio.run(
            server.call_tool("upsert_nodes", {"nodes": [{"label": "Person", "key": "ada"}]})
        )
        assert json.loads(res.content[0].text)["nodes"] == 1

        # expected failures arrive as readable output, not protocol errors
        res = asyncio.run(
            server.call_tool("cypher_query", {"cypher": "CREATE (p:Person {name: 'x'})"})
        )
        assert not res.is_error
        assert res.content[0].text.startswith("ERROR:")

        res = asyncio.run(
            server.call_tool(
                "search_knowledge", {"query": "ada", "labels": ["Person"]}
            )
        )
        assert not res.is_error
        assert "Person:ada" in res.content[0].text
    finally:
        server.grag_service.close()
