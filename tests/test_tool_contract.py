"""M17: public schema, query and error contracts through the real SDK and REST."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.errors import CypherError
from grag.core.revisions import content_revision
from grag.core.schema import build_schema_document
from grag.core.types import VECTOR_PROPS, QueryRequest
from grag.mcp_server.server import create_server


@pytest.fixture()
def surfaces(tmp_path):
    cfg = GragConfig(db_path=tmp_path / "contract.lbdb", buffer_pool_size=128 * 1024 * 1024,
                     auto_refresh_code=False)
    app = create_app(cfg)
    server = create_server(cfg, registry=app.state.registry)
    with TestClient(app) as client:
        svc = app.state.service
        schema = {"node_tables": [{"name": "Note", "properties": [{"name": "body"}]}],
                  "rel_tables": [{"name": "SUPPORTS", "from_label": "Note", "to_label": "Note"}]}
        assert client.post("/api/schema/define", json=schema).status_code == 200
        assert client.post("/api/nodes/upsert", json={
            "nodes": [{"label": "Note", "key": k, "properties": {"body": f"Evidence {k}"}, "source": "notes.md"}
                      for k in ["a", "b"]],
            "edges": [{"type": "SUPPORTS", "from_label": "Note", "from_key": "a",
                       "to_label": "Note", "to_key": "b", "source": "notes.md"}],
        }).status_code == 200
        yield svc, client, server


def invoke(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def text(result):
    return "\n".join(c.text for c in result.content if c.type == "text")


def footer(value):
    return json.loads(value.rsplit("\n---\n", 1)[-1])


def deref(schema, part):
    while "$ref" in part:
        part = schema["$defs"][part["$ref"].rsplit("/", 1)[-1]]
    return part


def test_nested_inputs_advertise_required_fields_types_and_revision_guards(surfaces):
    _, _, server = surfaces
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    schema = tools["define_schema"].input_schema
    node = deref(schema, schema["properties"]["node_tables"]["items"])
    assert node["required"] == ["name"] and node["additionalProperties"] is False
    prop = deref(schema, node["properties"]["properties"]["items"])
    assert "INT64" in prop["properties"]["type"]["enum"]
    rel = deref(schema, schema["properties"]["rel_tables"]["items"])
    assert set(rel["required"]) == {"name", "from_label", "to_label"}
    for tool, field, required in [("upsert_nodes", "nodes", {"label", "key"}),
                                  ("upsert_edges", "edges", {"type", "from_label", "from_key", "to_label", "to_key"})]:
        schema = tools[tool].input_schema
        item = deref(schema, schema["properties"][field]["items"])
        assert set(item["required"]) == required
        assert item["additionalProperties"] is False
        assert any("pattern" in variant for variant in item["properties"]["expected_revision"]["anyOf"])


@pytest.mark.parametrize("cypher", [
    "MATCH (a:Note)-[r:SUPPORTS]->(b:Note) RETURN a,r,b",
    "MATCH p=(a:Note)-[r:SUPPORTS]->(b:Note) RETURN p",
    "MATCH (a:Note)-[r:SUPPORTS]->(b:Note) RETURN {nodes: [a,b], edge: r}",
    "MATCH (n:Note) RETURN collect(n)",
])
def test_whole_entities_are_compact_across_transports_without_losing_evidence(surfaces, cypher):
    svc, client, server = surfaces
    svc.engine.execute_write("ALTER TABLE Note ADD embedding FLOAT[3]")
    svc.engine.execute_write("ALTER TABLE Note ADD _emb_model STRING")
    svc.engine.execute_write("MATCH (n:Note) SET n.embedding = [1.0,2.0,3.0], n._emb_model = 'test'")
    raw = svc.engine.execute(cypher)
    expected = {}

    def walk(value, *, compact=False):
        if isinstance(value, dict):
            if "_LABEL" in value and "_ID" in value:
                key = json.dumps(value["_ID"], sort_keys=True)
                if compact:
                    original = expected[key]
                    assert value["_revision"] == content_revision(original)
                    assert value == {**{k: v for k, v in original.items() if k not in VECTOR_PROPS},
                                     "_revision": content_revision(original)}
                else:
                    expected[key] = value
            else:
                for v in value.values():
                    walk(v, compact=compact)
        elif isinstance(value, list):
            for v in value:
                walk(v, compact=compact)

    walk(raw.rows)
    direct = svc.cypher_query(QueryRequest(cypher=cypher))
    walk(direct.rows, compact=True)
    assert {n.id for n in direct.subgraph.nodes} == {"Note:a", "Note:b"}
    assert all(n.properties["_source"] == "notes.md" for n in direct.subgraph.nodes)
    if "collect" not in cypher:
        edge = direct.subgraph.edges[0]
        assert (edge.source, edge.type, edge.target) == ("Note:a", "SUPPORTS", "Note:b")
        assert edge.properties["_revision"]
    rest = client.post("/api/query", json={"cypher": cypher})
    assert rest.status_code == 200
    assert rest.json() == direct.model_dump(mode="json")
    mcp = invoke(server, "cypher_query", {"cypher": cypher})
    assert not mcp.is_error and mcp.structured_content is None
    assert json.loads(text(mcp)) == direct.model_dump(mode="json", exclude={"subgraph"})
    # Returned guards still protect subsequent writes.
    revision = next(n for n in direct.subgraph.nodes if n.id == "Note:a").properties["_revision"]
    saved = invoke(server, "upsert_nodes", {"nodes": [{"label": "Note", "key": "a",
                   "properties": {"body": "Revised evidence"}, "expected_revision": revision}], "operation_id": "contract-edit"})
    assert not saved.is_error and json.loads(text(saved))["revisions"]["Note:a"] != revision


def test_explicit_vectors_and_user_maps_are_not_filtered(surfaces):
    svc, client, server = surfaces
    svc.engine.execute_write("ALTER TABLE Note ADD embedding FLOAT[3]")
    svc.engine.execute_write("MATCH (n:Note) SET n.embedding = [1.0,2.0,3.0]")
    cypher = "MATCH (n:Note {id:'a'}) RETURN n.embedding, {embedding:n.embedding, _emb_model:'authored'}"
    expected = [[[1.0, 2.0, 3.0], {"embedding": [1.0, 2.0, 3.0], "_emb_model": "authored"}]]
    assert svc.cypher_query(QueryRequest(cypher=cypher)).rows == expected
    assert client.post("/api/query", json={"cypher": cypher}).json()["rows"] == expected
    assert json.loads(text(invoke(server, "cypher_query", {"cypher": cypher})))["rows"] == expected


def test_schema_cache_reuses_views_but_never_freshness_or_uncommitted_data(surfaces, monkeypatch):
    svc, client, server = surfaces
    compact = svc.describe_schema(detail="compact")
    full = svc.describe_schema()
    assert compact.node_tables[0].row_count is None and not compact.node_tables[0].sample_keys
    assert full.node_tables[0].row_count == 2
    with monkeypatch.context() as patch:
        patch.setattr(svc.engine, "execute", lambda *a, **k: pytest.fail("cached schema must not query DB"))
        cached = svc.describe_schema(detail="compact", if_revision=compact.schema_revision)
        assert cached.unchanged and not cached.text and not cached.node_tables
        poisoned = svc.describe_schema(detail="compact")
        poisoned.node_tables.clear()
        poisoned.freshness.status = "error"
        assert svc.describe_schema(detail="compact").model_dump() == compact.model_dump()
    # Data edits invalidate full stats, but not the compact view's content hash.
    svc.engine.execute_write("CREATE (n:Note {id:'c'})")
    assert svc.describe_schema().node_tables[0].row_count == 3
    assert svc.describe_schema(detail="compact", if_revision=compact.schema_revision).unchanged
    with pytest.raises(RuntimeError, match="rollback"), svc.engine.write_transaction():
        svc.engine.execute_write("CREATE (n:Note {id:'rolled-back'})")
        assert svc.describe_schema().node_tables[0].row_count == 4
        raise RuntimeError("rollback")
    assert svc.describe_schema().node_tables[0].row_count == 3
    svc.engine.execute_write("ALTER TABLE Note ADD priority INT64")
    assert not svc.describe_schema(detail="compact", if_revision=compact.schema_revision).unchanged
    # Same detail -> identical metadata on MCP, REST JSON and REST text.
    for detail in ["compact", "full"]:
        mcp = invoke(server, "describe_schema", {"detail": detail})
        doc = client.get("/api/schema", params={"detail": detail}).json()
        rendered = client.get("/api/schema", params={"detail": detail, "format": "text"}).text
        assert text(mcp) == rendered
        assert footer(rendered) == {k: doc[k] for k in ["detail", "schema_revision", "unchanged", "freshness"]}
        args = {"detail": detail, "if_revision": doc["schema_revision"]}
        assert footer(text(invoke(server, "describe_schema", args)))["unchanged"]
        assert client.get("/api/schema", params=args).json()["unchanged"]
    # A cached schema is never an exemption from require-fresh reads.
    failure = invoke(server, "describe_schema", {"if_revision": compact.schema_revision, "freshness": "require"})
    assert failure.is_error and failure.structured_content["code"] == "freshness_unavailable"


def test_compact_schema_omits_only_vectors_and_never_scans_counts(surfaces, monkeypatch):
    from grag.core import schema

    svc, _, server = surfaces
    svc.engine.execute_write("ALTER TABLE Note ADD embedding FLOAT[3]")
    monkeypatch.setattr(schema, "_row_count", lambda *args: pytest.fail("compact counts scan"))
    monkeypatch.setattr(schema, "_sample_keys", lambda *args: pytest.fail("compact samples scan"))
    reply = text(invoke(server, "describe_schema", {}))
    assert "embedding" not in reply and "_source:STRING" in reply and "id:STRING PK" in reply
    assert "SUPPORTS(Note -> Note" in reply and footer(reply)["detail"] == "compact"
    svc.engine.execute_write("CREATE NODE TABLE Other(id STRING PRIMARY KEY)")
    monkeypatch.setattr(svc.engine, "execute", lambda *args: (_ for _ in ()).throw(CypherError("catalog failed")))
    with pytest.raises(CypherError, match="catalog failed"):
        build_schema_document(svc.engine, svc.config, detail="compact")
    assert not svc.engine.schema_cache


@pytest.mark.parametrize(("tool", "args", "route", "status", "code"), [
    ("upsert_nodes", {"nodes": [{"label": "Note"}]}, "/api/nodes/upsert", 422, "validation_error"),
    ("define_schema", {"node_tables": [{"name": "Bad", "properties": [{"name": "age", "type": "secret-invalid-value"}]}], "rel_tables": []}, "/api/schema/define", 422, "validation_error"),
    ("cypher_query", {"cypher": "DELETE n"}, "/api/query", 403, "read_only_violation"),
    ("cypher_query", {"cypher": "MATCH (n:Missing) RETURN n"}, "/api/query", 400, "cypher_error"),
    ("upsert_nodes", {"nodes": [{"label": "Missing", "key": "a"}]}, "/api/nodes/upsert", 400, "schema_error"),
    ("upsert_edges", {"edges": [{"type": "SUPPORTS", "from_label": "Note", "from_key": "missing", "to_label": "Note", "to_key": "b"}]}, "/api/edges/upsert", 404, "not_found"),
    ("upsert_nodes", {"nodes": [{"label": "Note", "key": "a", "expected_revision": "absent"}]}, "/api/nodes/upsert", 409, "revision_conflict"),
    ("cypher_query", {"cypher": "RETURN 1", "freshness": "require"}, "/api/query", 503, "freshness_unavailable"),
])
def test_errors_have_codes_and_survive_mcp_and_rest(surfaces, tool, args, route, status, code):
    _, client, server = surfaces
    mcp = invoke(server, tool, args)
    rest = client.post(route, json=args)
    assert mcp.is_error and rest.status_code == status, (text(mcp), rest.text)
    payload = mcp.structured_content
    assert payload["code"] == rest.json()["code"] == code
    assert payload == footer(text(mcp))
    assert "secret-invalid-value" not in text(mcp) + rest.text
    if code != "validation_error":
        assert payload == rest.json()
    else:
        assert payload["details"] and rest.json()["details"]
        assert all("input" not in detail for detail in payload["details"])


def test_unexpected_mcp_errors_are_coded_and_redacted(surfaces, monkeypatch):
    svc, _, server = surfaces

    def broken(*args, **kwargs):
        raise RuntimeError("private-driver-detail")

    monkeypatch.setattr(svc, "cypher_query", broken)
    result = invoke(server, "cypher_query", {"cypher": "RETURN 1"})
    assert result.is_error and result.structured_content["code"] == "internal_error"
    assert "private-driver-detail" not in text(result)


@pytest.mark.parametrize("failed_query", ["SHOW_TABLES", "MATCH (m:_grag_tables)", "TABLE_INFO", "SHOW_CONNECTION", "count(n)", "LIMIT 5"])
def test_transient_schema_errors_never_become_cached_incomplete_views(surfaces, monkeypatch, failed_query):
    svc, _, _ = surfaces
    assert not svc.engine.schema_cache
    execute = svc.engine.execute

    def temporarily_broken(query, *args, **kwargs):
        if failed_query in query:
            raise CypherError("transient catalog read failure")
        return execute(query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(svc.engine, "execute", temporarily_broken)
        with pytest.raises(CypherError, match="transient"):
            svc.describe_schema()
        assert not svc.engine.schema_cache
    recovered = svc.describe_schema()
    assert recovered.node_tables[0].row_count == 2
    assert recovered.rel_tables[0].from_label == "Note"


def test_evidence_lifecycle_history_and_validation_across_rest_mcp(surfaces):
    _, client, server = surfaces
    schema = {t.name:t for t in asyncio.run(server.list_tools())}
    assert 'EvidenceUpdate' in schema['upsert_nodes'].input_schema['$defs']
    assert schema['search_knowledge'].input_schema['properties']['evidence']['enum']==['current','all']
    assert {'history','history_before','revision'} <= schema['get_context'].input_schema['properties'].keys()
    before=client.post('/api/query',json={'cypher':"MATCH (n:Note {id:'a'}) RETURN n"}).json()['rows'][0][0]
    request={'operation_id':'review-1','nodes':[{'label':'Note','key':'a','expected_revision':before['_revision'],
             'properties':{'body':'Corrected evidence'},'source':'review.md',
             'evidence':{'actor':'Claude Code','reason':'Source correction','review':'accepted'}}]}
    saved=invoke(server,'upsert_nodes',request)
    assert not saved.is_error, text(saved)
    after=client.post('/api/query',json={'cypher':"MATCH (n:Note {id:'a'}) RETURN n"}).json()['rows'][0][0]
    assert after['_evidence_seq']==1 and after['_revision']!=before['_revision']
    history_request={'node_ids':['Note:a'],'history':True,'token_budget':3000}
    rest=client.post('/api/context',json=history_request).json()
    mcp=footer(text(invoke(server,'get_context',history_request)))
    assert rest['history']==mcp['history']
    assert [e['sequence'] for e in rest['history']['entries']]==[1,0]
    assert rest['history']['entries'][0]['actor']=='Claude Code'
    revision_request={'node_ids':['Note:a'],'revision':0,'text_property':'body','token_budget':1500}
    rest=client.post('/api/context',json=revision_request).json()
    old=text(invoke(server,'get_context',revision_request))
    assert rest['context'] in old and 'Evidence a' in old and '_history_revision: 0' in old
    disputed={'nodes':[{'label':'Note','key':'a','expected_revision':after['_revision'],
                        'evidence':{'actor':'Cursor','review':'disputed','reason':'Sources conflict'}}]}
    assert client.post('/api/nodes/upsert',json=disputed).status_code==200
    filtered=client.post('/api/context',json={'node_ids':['Note:a']}).json()
    assert not filtered['included_node_ids'] and filtered['evidence_policy']=='current'
    all_text=text(invoke(server,'get_context',{'node_ids':['Note:a'],'evidence':'all','token_budget':3000}))
    assert 'Corrected evidence' in all_text and 'disputed' in all_text
    invalid={'nodes':[{'label':'Note','key':'b','evidence':{'expires_at':'2026-09-01T12:00:00'}}]}
    assert client.post('/api/nodes/upsert',json=invalid).status_code==422
    refused=invoke(server,'upsert_nodes',invalid)
    assert refused.is_error and footer(text(refused))['code']=='validation_error'
    for invalid in [{'node_ids':['Note:a'],'history_before':2},
                    {'node_ids':['Note:a'],'history':True,'revision':0},
                    {'node_ids':['Note:a','Note:b'],'revision':0}]:
        assert client.post('/api/context',json=invalid).status_code==422
        assert invoke(server,'get_context',invalid).is_error
