"""Routine agent reads stay small without losing exact values or evidence."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from grag.config import GragConfig
from grag.core.types import QueryRequest
from grag.mcp_server import server as mcp
from grag.service import GragService


@pytest.fixture()
def graph(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "cost.lbdb", buffer_pool_size=128 * 1024**2))
    assert not mcp.define_schema(svc, [{"name": "Note", "primary_key": "slug", "properties": [
        {"name": "body"}, {"name": "spare"}, {"name": "count", "type": "INT64"},
        {"name": "enabled", "type": "BOOL"},
    ]}], []).startswith("ERROR")
    assert not mcp.upsert_nodes(svc, [{"label": "Note", "key": "n", "properties": {
        "body": "", "count": 0, "enabled": False,
    }, "source": "policy.md", "evidence": {}}]).startswith("ERROR")
    try:
        yield svc
    finally:
        svc.close()


@pytest.mark.parametrize("query,expected", [
    ("MATCH (n:Note) RETURN count(n)", [[1]]),
    ("MATCH (n:Note) RETURN n.spare", [[None]]),
    ("MATCH (n:Note {slug: 'absent'}) RETURN n", []),
    ("MATCH (n:Note) RETURN {value: n.spare, items: [n.spare]}", [[{"value": None, "items": [None]}]]),
])
def test_scalar_and_empty_queries_skip_catalog_but_keep_exact_results(graph, monkeypatch, query, expected):
    monkeypatch.setattr(graph, "_pk_map", lambda: pytest.fail("scalar read must not walk the catalog"))
    reply = json.loads(mcp.cypher_query(graph, query))
    assert reply["rows"] == expected
    assert reply["truncated"] is False
    assert "freshness" in reply


def test_whole_mcp_entities_omit_only_nulls_preserving_revision_and_projections(graph):
    query = "MATCH (n:Note) RETURN {node: n, projected: n.spare, items: [n.spare]}"
    service_reply = graph.cypher_query(QueryRequest(cypher=query))
    assert service_reply.subgraph.nodes[0].id == "Note:n"  # custom PK still resolved
    original = service_reply.model_dump(mode="json")["rows"][0][0]["node"]
    assert original["spare"] is None  # REST/Python exact rows unchanged
    reply = json.loads(mcp.cypher_query(graph, query))["rows"][0][0]
    assert reply["projected"] is None and reply["items"] == [None]
    node = reply["node"]
    assert node == {k: v for k, v in original.items() if v is not None}
    assert node["body"] == "" and node["count"] == 0 and node["enabled"] is False
    assert node["_source"] == "policy.md" and node["_review_state"] == "unreviewed"
    updated = json.loads(mcp.upsert_nodes(graph, [{"label": "Note", "key": "n",
        "properties": {"body": "revised"}, "expected_revision": node["_revision"]}]))
    assert updated["nodes"] == 1


def test_bm25_first_search_does_not_import_numeric_or_model_runtime(tmp_path):
    script = textwrap.dedent("""
        import json, sys
        from grag.config import GragConfig
        from grag.service import GragService
        from grag.mcp_server import server as mcp
        svc = GragService(GragConfig(db_path=sys.argv[1], buffer_pool_size=128*1024**2))
        try:
            mcp.define_schema(svc, [{'name':'Note', 'properties':[{'name':'body'}]}], [])
            mcp.upsert_nodes(svc, [{'label':'Note','key':'n','source':'policy.md',
                                   'properties':{'body':'Cache policy'}}])
            result = mcp.search_knowledge(svc, 'cache', top_k=1, hops=0)
            assert 'Note:n' in result and 'policy.md' in result, result
            assert json.loads(result.split('\\n---\\n')[-1])['vector'] == 'off', result
            print(json.dumps([m for m in ('numpy', 'fastembed', 'onnxruntime',
                'torch', 'sentence_transformers', 'grag.retrieval.polar') if m in sys.modules]))
        finally:
            svc.close()
    """)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "bm25.lbdb")],  # noqa: S603
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_total_mcp_discovery_budget(tmp_path):
    server = mcp.create_server(GragConfig(db_path=tmp_path / "discovery.lbdb"))
    async def sizes():
        tools = await server.list_tools()
        assert len(tools) == 10
        wire = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        return len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode())
    # Guard the entire advertised tool list (including schemas), rather than
    # exact prose. Baseline was 31,682 bytes plus 2,088 instruction bytes.
    try:
        assert asyncio.run(sizes()) + len(mcp._INSTRUCTIONS.encode()) < 24_000
    finally:
        server.grag_service.close()
