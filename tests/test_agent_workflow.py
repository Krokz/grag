"""Exercise an agent's memory loop through real MCP stdio and CLI processes.

Run with: PYTHONPATH=src python -m pytest tests/test_agent_workflow.py
Set GRAG_TEST_COMMAND to an installed grag launcher to exercise that command
without injecting the checkout into its Python path.
Only temporary source files and a temporary database are written.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {
    "describe_schema",
    "define_schema",
    "upsert_nodes",
    "upsert_edges",
    "cypher_query",
    "search_knowledge",
    "get_context",
    "ingest_code",
    "ingest_docs",
    "job_status",
}


def test_agent_memory_loop_over_stdio(tmp_path):
    asyncio.run(_memory_loop(tmp_path))


async def _memory_loop(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    source = tmp_path / "example.py"
    source.write_text(
        'def remember():\n    """Save useful local context."""\n    pass\n'
    )
    provenance = str(tmp_path / "session.md")
    launcher = os.environ.get("GRAG_TEST_COMMAND")
    arguments = ["--db", str(tmp_path / "memory.lbdb"), "mcp"]
    server_env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
        "GRAG_EMBED_PROVIDER": "",
        "GRAG_BUFFER_POOL_MB": "128",
        "GRAG_AUTO_REFRESH_INTERVAL_S": "0",
    }
    if not launcher:
        server_env["PYTHONPATH"] = str(checkout / "src")
    params = StdioServerParameters(
        command=launcher or sys.executable,
        args=arguments if launcher else ["-m", "grag.cli", *arguments],
        # Launch outside the checkout to verify explicit source/runtime selection.
        cwd=str(tmp_path),
        env=server_env,
    )

    @asynccontextmanager
    async def connect():
        with (tmp_path / "mcp.stderr").open("a") as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=30
                ) as session:
                    await session.initialize()
                    yield session

    async def call(session, name, arguments, *, error=None):
        result = await session.call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if c.type == "text")
        if error:
            assert error in text, text
        else:
            assert not result.is_error and not text.startswith("ERROR:"), text
        return text

    async def query(session, cypher):
        return json.loads(
            await call(
                session,
                "cypher_query",
                {
                    "cypher": cypher,
                    "freshness": "require",
                    "freshness_timeout_ms": 10000,
                },
            )
        )

    async with connect() as session:
        assert {t.name for t in (await session.list_tools()).tools} == TOOLS
        await call(session, "describe_schema", {})
        await call(session, "ingest_code", {"paths": [str(source)]})
        await call(session, "describe_schema", {})
        indexed = await query(
            session, "MATCH (f:Function) RETURN f.id, f.line_start, f._source"
        )
        assert indexed["freshness"]["status"] == "fresh"
        function_id, line, path = indexed["rows"][0]
        assert path == str(source) and source.read_text().splitlines()[
            line - 1
        ].startswith("def remember")
        await call(
            session,
            "define_schema",
            {
                "node_tables": [
                    {
                        "name": "Task",
                        "primary_key": "id",
                        "properties": [
                            {"name": "title"},
                            {"name": "status"},
                            {"name": "body"},
                        ],
                    },
                    {
                        "name": "Decision",
                        "primary_key": "name",
                        "properties": [{"name": "summary"}],
                    },
                ],
                "rel_tables": [
                    {"name": "GUIDED_BY", "from_label": "Task", "to_label": "Decision"},
                    {
                        "name": "DOCUMENTS_FUNCTION",
                        "from_label": "Decision",
                        "to_label": "Function",
                    },
                ],
            },
        )
        batch = {
            "operation_id": "session-memory-1",
            "nodes": [
                {
                    "label": "Task",
                    "key": "next",
                    "source": provenance,
                    "properties": {
                        "title": "Verify session continuity",
                        "status": "open",
                        "body": "Check code citations",
                    },
                },
                {
                    "label": "Decision",
                    "key": "Keep local memory simple",
                    "source": provenance,
                    "properties": {
                        "summary": "Use one local graph to remember useful context between sessions."
                    },
                },
            ],
            "edges": [
                {
                    "type": "GUIDED_BY",
                    "from_label": "Task",
                    "from_key": "next",
                    "to_label": "Decision",
                    "to_key": "Keep local memory simple",
                    "source": provenance,
                },
                {
                    "type": "DOCUMENTS_FUNCTION",
                    "from_label": "Decision",
                    "from_key": "Keep local memory simple",
                    "to_label": "Function",
                    "to_key": function_id,
                    "source": provenance,
                },
            ],
        }
        saved = json.loads(await call(session, "upsert_nodes", batch))
        assert (
            saved["nodes"],
            saved["edges"],
            saved["warnings"],
            saved["replayed"],
        ) == (2, 2, [], False)
        search = await call(
            session,
            "search_knowledge",
            {
                "query": "why keep local memory simple",
                "labels": ["Decision"],
                "top_k": 1,
                "hops": 1,
                "token_budget": 3000,
            },
        )
        context, footer = search.rsplit("\n---\n", 1)
        metadata = json.loads(footer)
        assert "Keep local memory simple" in context and "DOCUMENTS_FUNCTION" in context
        assert provenance in context and str(source) in context
        assert metadata["vector"] == "off"
        assert metadata["response_token_estimate"] <= 3000
        assert not metadata["truncated"]
        task = (await query(session, "MATCH (t:Task) RETURN t"))["rows"][0][0]
        await call(
            session,
            "upsert_nodes",
            {
                "nodes": [
                    {
                        "label": "Task",
                        "key": "next",
                        "source": provenance,
                        "properties": {
                            "body": "Citations verified; resume after restart"
                        },
                        "expected_revision": task["_revision"],
                    }
                ]
            },
        )
        await call(
            session,
            "upsert_nodes",
            {
                "nodes": [
                    {
                        "label": "Task",
                        "key": "must-not-exist",
                        "properties": {},
                        "source": provenance,
                    },
                    {
                        "label": "Task",
                        "key": "next",
                        "properties": {"body": "stale edit"},
                        "expected_revision": task["_revision"],
                        "source": provenance,
                    },
                ]
            },
            error="CODE: revision_conflict",
        )
        assert (
            await query(
                session, "MATCH (t:Task) WHERE t.id = 'must-not-exist' RETURN t.id"
            )
        )["rows"] == []
        assert json.loads(await call(session, "upsert_nodes", batch))["replayed"]
        # Move the function's line while retaining identity and the authored link.
        source.write_text(
            '# source changed after indexing\n\ndef remember():\n    """Save useful local context."""\n    pass\n'
        )
        updated = await query(session, "MATCH (f:Function) RETURN f.id, f.line_start")
        assert updated["freshness"]["status"] == "fresh"
        assert updated["rows"] == [[function_id, 3]]

    # A new CLI process must reopen the same graph, receipt, policy and relationships.
    async with connect() as session:
        await call(session, "describe_schema", {})
        assert json.loads(await call(session, "upsert_nodes", batch))["replayed"]
        resumed = await query(
            session,
            "MATCH (t:Task)-[:GUIDED_BY]->(d:Decision)-[:DOCUMENTS_FUNCTION]->(f:Function) WHERE t.status = 'open' RETURN t.body, d.name, f.line_start",
        )
        assert resumed["rows"] == [
            ["Citations verified; resume after restart", "Keep local memory simple", 3]
        ]
        assert resumed["freshness"]["status"] == "fresh"
        context = await call(
            session,
            "get_context",
            {
                "node_ids": ["Decision:Keep local memory simple"],
                "hops": 1,
                "token_budget": 3000,
                "freshness": "require",
                "freshness_timeout_ms": 10000,
            },
        )
        assert "DOCUMENTS_FUNCTION" in context and provenance in context
