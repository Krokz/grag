"""Tests for grag.mcp_server.server — plain tool functions (full LLM workflow
simulation, error contract) plus FastMCP/MCPServer registration of the frozen
tool names, multi-db per-call routing, and the streamable-http app."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, NotFoundError
from grag.mcp_server import server as mcp_server
from grag.service import GragService

POOL_128MB = 128 * 1024 * 1024

FROZEN_TOOLS = {
    "describe_schema",
    "define_schema",
    "upsert_nodes",
    "upsert_edges",
    "cypher_query",
    "search_knowledge",
    "get_context",
    "ingest_code",
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
    {
        "label": "Person",
        "key": "grace",
        "properties": {"age": 85},
        "source": "people.md",
    },
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
    payload = json.loads(footer or search_out)
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

    full = json.loads(
        mcp_server.cypher_query(service, "MATCH (p:Person) RETURN p.name")
    )
    assert full["row_count"] == 3
    assert full["truncated"] is False


def test_cypher_query_rejects_write_keywords(service: GragService):
    _define(service)
    out = mcp_server.cypher_query(service, "CREATE (p:Person {name: 'mallory'})")
    assert out.startswith("ERROR:")
    assert "read-only" in out
    assert "HINT:" in out and "upsert" in out
    # guardrail fired before anything was written
    check = json.loads(
        mcp_server.cypher_query(service, "MATCH (p:Person) RETURN count(p)")
    )
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
    rows = json.loads(
        mcp_server.cypher_query(service, "MATCH (p:Person) RETURN p.age")
    )["rows"]
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
    # No embedder configured on the test service: "vector":"off" disambiguates
    # this from "embedder configured, backlog fully drained" (both would
    # otherwise look identical — no pending_embeddings, no vector seeds).
    assert payload == {"seeds": [], "vector": "off"}


# --- ingest_code ---------------------------------------------------------------------


def test_ingest_code_tool(service: GragService, tmp_path):
    from grag.ingest.code import _repo_id

    pkg = tmp_path / "mcpkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        'def f(x: int) -> int:\n    """Identity-ish."""\n    return x\n',
        encoding="utf-8",
    )

    out = mcp_server.ingest_code(service, [str(pkg)])
    assert not out.startswith("ERROR"), out
    payload = json.loads(out)
    assert payload["repos"] == 1
    assert payload["modules"] == 1
    assert payload["functions"] == 1
    assert payload["warnings"] == []

    # the code graph is queryable through the existing tools
    result = json.loads(
        mcp_server.cypher_query(service, "MATCH (f:Function) RETURN f.id, f.signature")
    )
    assert result["rows"] == [[f"{_repo_id(pkg)}:mod.py#f", "def f(x: int) -> int:"]]
    assert "Module" in mcp_server.describe_schema(service)

    # re-run is idempotent
    again = json.loads(mcp_server.ingest_code(service, [str(pkg)]))
    assert again["modules"] == 1
    check = json.loads(
        mcp_server.cypher_query(service, "MATCH (m:Module) RETURN count(m)")
    )
    assert check["rows"] == [[1]]


def test_ingest_code_validation_error_returns_error_string(service: GragService):
    out = mcp_server.ingest_code(service, "not-a-list")  # paths must be a list
    assert isinstance(out, str)  # returned, not raised
    assert out.startswith("ERROR:")
    assert "paths" in out
    assert "HINT:" in out


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
            server.call_tool(
                "upsert_nodes", {"nodes": [{"label": "Person", "key": "ada"}]}
            )
        )
        assert json.loads(res.content[0].text)["nodes"] == 1

        # expected failures arrive as readable output, not protocol errors
        res = asyncio.run(
            server.call_tool(
                "cypher_query", {"cypher": "CREATE (p:Person {name: 'x'})"}
            )
        )
        assert not res.is_error
        assert res.content[0].text.startswith("ERROR:")

        res = asyncio.run(
            server.call_tool("search_knowledge", {"query": "ada", "labels": ["Person"]})
        )
        assert not res.is_error
        assert "Person:ada" in res.content[0].text
    finally:
        server.grag_service.close()


# --- multi-db routing -----------------------------------------------------------------


class _StubCtx:
    """Minimal ctx stand-in: _resolve_service only reads ctx.headers."""

    def __init__(self, headers):
        self._headers = headers

    @property
    def headers(self):
        return self._headers


def _make_db(path: Path, table_name: str) -> None:
    svc = GragService(GragConfig(db_path=path, buffer_pool_size=POOL_128MB))
    try:
        out = mcp_server.define_schema(
            svc, [{"name": table_name, "primary_key": "id"}], []
        )
        assert not out.startswith("ERROR"), out
    finally:
        svc.close()


@pytest.fixture()
def multi_db_dir(tmp_path):
    db_dir = tmp_path / "dbs"
    db_dir.mkdir()
    _make_db(db_dir / "alpha.lbdb", "AlphaThing")
    _make_db(db_dir / "beta.lbdb", "BetaThing")
    return db_dir


def _multi_db_config(multi_db_dir: Path) -> GragConfig:
    # db_path names the default database resolved when no header is present
    return GragConfig(
        db_dir=multi_db_dir, db_path=Path("alpha.lbdb"), buffer_pool_size=POOL_128MB
    )


def test_resolve_service_routes_by_x_grag_db_header(multi_db_dir):
    server = mcp_server.create_server(_multi_db_config(multi_db_dir))
    try:
        registry = server.grag_registry

        # no ctx -> default database (alpha, via db_path name)
        default_svc = mcp_server._resolve_service(registry, None)
        alpha_schema = mcp_server.describe_schema(default_svc)
        assert "AlphaThing" in alpha_schema
        assert "BetaThing" not in alpha_schema

        # the x-grag-db header routes to the named database
        beta_svc = mcp_server._resolve_service(
            registry, _StubCtx({"x-grag-db": "beta"})
        )
        assert beta_svc is not default_svc
        beta_schema = mcp_server.describe_schema(beta_svc)
        assert "BetaThing" in beta_schema
        assert "AlphaThing" not in beta_schema

        # empty/absent headers behave like no ctx
        assert mcp_server._resolve_service(registry, _StubCtx({})) is default_svc
        assert mcp_server._resolve_service(registry, _StubCtx(None)) is default_svc

        # unknown db name -> NotFoundError listing the available databases
        with pytest.raises(NotFoundError):
            mcp_server._resolve_service(registry, _StubCtx({"x-grag-db": "ghost"}))
    finally:
        server.grag_registry.close()


def test_tool_closure_applies_error_contract_to_routing(multi_db_dir):
    server = mcp_server.create_server(_multi_db_config(multi_db_dir))
    try:
        # the registered describe_schema closure, driven with a stub ctx
        tool_fn = server._tool_manager._tools["describe_schema"].fn

        beta_out = tool_fn(ctx=_StubCtx({"x-grag-db": "beta"}))
        assert not beta_out.startswith("ERROR")
        assert "BetaThing" in beta_out

        # routing failures arrive as readable ERROR/HINT output, not raises
        ghost_out = tool_fn(ctx=_StubCtx({"x-grag-db": "ghost"}))
        assert ghost_out.startswith("ERROR:")
        assert "ghost" in ghost_out
        assert "HINT:" in ghost_out
        assert "alpha" in ghost_out and "beta" in ghost_out
    finally:
        server.grag_registry.close()


def test_mcp_call_tool_multi_db_uses_default_without_headers(multi_db_dir):
    # call_tool outside a request injects a Context whose .headers is
    # unavailable; the guard must fall back to the default database.
    server = mcp_server.create_server(_multi_db_config(multi_db_dir))
    try:
        res = asyncio.run(server.call_tool("describe_schema", {}))
        assert not res.is_error
        assert "AlphaThing" in res.content[0].text
        assert "BetaThing" not in res.content[0].text
    finally:
        server.grag_registry.close()


def test_resolve_service_single_db_mode(tmp_path):
    server = mcp_server.create_server(
        GragConfig(db_path=tmp_path / "single.lbdb", buffer_pool_size=POOL_128MB)
    )
    try:
        # no ctx -> the one service, same object as the back-compat handle
        svc = mcp_server._resolve_service(server.grag_registry, None)
        assert svc is server.grag_service
        # passing a db name in single-db mode is a ConfigurationError
        with pytest.raises(ConfigurationError):
            mcp_server._resolve_service(
                server.grag_registry, _StubCtx({"x-grag-db": "other"})
            )
    finally:
        server.grag_registry.close()


# --- streamable-http transport ---------------------------------------------------------


def test_streamable_http_app_builds_without_serving(tmp_path):
    from starlette.applications import Starlette

    server = mcp_server.create_server(
        GragConfig(db_path=tmp_path / "http.lbdb", buffer_pool_size=POOL_128MB)
    )
    try:
        app = server.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True
        )
        assert isinstance(app, Starlette)
    finally:
        server.grag_registry.close()


def test_standalone_http_rejects_unauthenticated_non_loopback_bind(tmp_path):
    config = GragConfig(
        db_path=tmp_path / "must-not-be-created.lbdb",
        buffer_pool_size=POOL_128MB,
    )

    with pytest.raises(ConfigurationError, match="GRAG_API_TOKEN"):
        mcp_server._validate_standalone_http_security(
            config,
            "0.0.0.0",  # noqa: S104 — deliberately test unsafe binding
        )

    assert not config.db_path.exists()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_standalone_http_allows_unauthenticated_loopback(host, tmp_path):
    config = GragConfig(db_path=tmp_path / "loopback.lbdb")
    mcp_server._validate_standalone_http_security(config, host)


def test_standalone_http_bearer_middleware():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def ok(_request):
        return PlainTextResponse("ok")

    app = mcp_server._BearerAuthMiddleware(
        Starlette(routes=[Route("/mcp", ok)]),
        "sekret",
    )
    client = TestClient(app)

    assert client.get("/mcp").status_code == 401
    wrong = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401
    response = client.get("/mcp", headers={"Authorization": "Bearer sekret"})
    assert response.status_code == 200
    assert response.text == "ok"
