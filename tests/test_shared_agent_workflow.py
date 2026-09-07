"""Two real MCP proxies share one daemon; optional real, offline embeddings.

GRAG_TEST_COMMAND selects an installed launcher. Set GRAG_WORKFLOW_EMBEDDINGS=1
for a locally cached fastembed model; CI uses FTS without an extra dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from grag.config import GragConfig, database_identity
from grag.core.engine import Engine
from workflow_eval import percentile95


def test_two_agents_share_memory_and_restart(tmp_path):
    asyncio.run(shared_workflow(tmp_path))


async def shared_workflow(root):
    started = time.perf_counter()
    embeddings = os.environ.get("GRAG_WORKFLOW_EMBEDDINGS") == "1"
    batches, doc_count = 3, 12
    db = root / "memory.lbdb"
    docs = root / "docs"
    docs.mkdir()
    for i in range(doc_count):
        (docs / f"{i}.md").write_text(f"# Note {i}\n\nShared project memory.\n\n## Behavior\n\nKeep useful context durable.\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    launcher = os.environ.get("GRAG_TEST_COMMAND")
    command = [launcher] if launcher else [sys.executable, "-m", "grag.cli"]
    env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""),
           "GRAG_EMBED_PROVIDER": "fastembed" if embeddings else "", "GRAG_AUTO_REFRESH_CODE": "0",
           "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "GRAG_BUFFER_POOL_MB": "128"}
    if not launcher:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    http = build_opener(ProxyHandler({}))
    latencies = {}

    async def call(session, name, args, *, allow_error=False):
        start = time.perf_counter()
        result = await session.call_tool(name, args)
        latencies.setdefault(name, []).append((time.perf_counter() - start) * 1000)
        text = "\n".join(c.text for c in result.content if c.type == "text")
        if result.is_error:
            assert allow_error, text
            assert result.structured_content == json.loads(text.rsplit("\n---\n", 1)[-1])
            assert result.structured_content["code"]
        if not allow_error:
            assert not result.is_error and not text.startswith("ERROR:"), text
        return text

    def health():
        with http.open(f"http://127.0.0.1:{port}/api/health", timeout=10) as response:
            data = json.load(response)
        assert data["database_id"] == database_identity(db) and data["mcp_enabled"]
        return data

    async def ready(session):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            text = await call(session, "search_knowledge", {"query": "shared project", "labels": ["Memory"], "hops": 0, "top_k": 2})
            footer = json.loads(text.rsplit("\n---\n", 1)[1])
            status = (await asyncio.to_thread(health))["embedding"]
            if not embeddings:
                assert footer["vector"] == "off" and status is None
                return None
            assert footer.get("vector") not in ("off", "error"), footer
            assert status["last_error"] is None, status
            if not footer.get("pending_embeddings", 0) and status["idle"]:
                return status
            await asyncio.sleep(0.1)
        raise AssertionError("Embedding backlog did not drain within 120 seconds")

    params = StdioServerParameters(command=command[0], args=[*command[1:], "--db", str(db), "mcp", "--auto-serve", "--port", str(port)], cwd=str(root), env=env)

    @asynccontextmanager
    async def connect():
        with (root / "shared-mcp.stderr.log").open("a") as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=60) as session:
                    await session.initialize()
                    await call(session, "describe_schema", {})
                    yield session

    def stop():
        result = subprocess.run(  # noqa: S603 — command is the selected test runtime
            [*command, "--db", str(db), "stop"], env=env, cwd=root,
            text=True, capture_output=True, timeout=45, check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    def batch(client, number, revision="initial"):
        return {"operation_id": f"{client}:batch:{number}:{revision}", "nodes": [
            {"label": "Memory", "key": f"{client}-{number}-{i}", "source": str(docs / "0.md"),
             "properties": {"body": f"Shared project context: client {client}, batch {number}, note {i}, revision {revision}"}}
            for i in range(10)]}

    async def writer(session, client, revision="initial"):
        for number in range(batches):
            result = json.loads(await call(session, "upsert_nodes", batch(client, number, revision)))
            assert not result["warnings"], result
            await call(session, "search_knowledge", {"query": "shared project", "labels": ["Memory"], "hops": 0, "top_k": 2})

    try:
        async with connect() as a:
            await call(a, "define_schema", {"node_tables": [{"name": "Memory", "primary_key": "id", "properties": [{"name": "body"}]}], "rel_tables": []})
            await call(a, "upsert_nodes", {"nodes": [{"label": "Memory", "key": "shared", "source": str(docs / "0.md"), "properties": {"body": "initial shared revision"}}]})
            async with connect() as b:
                # M17: the stdio -> HTTP relay preserves typed input schemas,
                # conditional schema replies, and structured tool errors.
                listed = {t.name: t for t in (await b.list_tools()).tools}
                assert "UpsertNode" in listed["upsert_nodes"].input_schema["$defs"]
                schema = json.loads((await call(a, "describe_schema", {})).rsplit("\n---\n", 1)[-1])
                cached = json.loads(await call(b, "describe_schema", {"if_revision": schema["schema_revision"]}))
                assert cached["unchanged"] and cached["schema_revision"] == schema["schema_revision"]
                invalid = await call(b, "upsert_nodes", {"nodes": [{"label": "Memory"}]}, allow_error=True)
                assert json.loads(invalid.rsplit("\n---\n", 1)[-1])["code"] == "validation_error"
                initial = await asyncio.gather(writer(a, "claude-shaped"), writer(b, "cursor-shaped"), call(a, "ingest_docs", {"paths": [str(docs)]}))
                assert json.loads(initial[2])["documents"] == doc_count
                first = await ready(a)
                for document in docs.glob("*.md"):
                    document.write_text(document.read_text() + "\nRevised context tests document embedding invalidation.\n")
                await asyncio.gather(writer(a, "claude-shaped", "updated"), writer(b, "cursor-shaped", "updated"), call(b, "ingest_docs", {"paths": [str(docs)]}))
                second = await ready(a)
                if embeddings:
                    assert second["embedded_total"] > first["embedded_total"]
                count = json.loads(await call(a, "cypher_query", {"cypher": "MATCH (n:Memory) WHERE n.body ENDS WITH 'revision updated' RETURN count(n)"}))["rows"]
                assert count == [[20 * batches]]
                # M27: the actual relay carries exact excerpt coordinates and
                # the existing pager accepts their hash, then rejects edits.
                long_body = ("Background notes for ordinary rotation. " * 150 +
                             "Service accounts must reauthenticate immediately after signing-key rotation.")
                await call(a, "upsert_nodes", {"nodes": [{"label": "Memory", "key": "shared",
                           "properties": {"body": long_body}, "source": str(docs / "0.md")}]})
                # Let the existing background worker finish this revision
                # before judging retrieval (the workflow also tests pending
                # embeddings during the concurrent writes above).
                await ready(a)
                excerpt_text = await call(b, "search_knowledge", {"query": "service accounts signing-key rotation",
                                          "labels": ["Memory"], "top_k": 1, "hops": 0, "token_budget": 1500})
                excerpt_meta = json.loads(excerpt_text.rsplit("\n---\n", 1)[-1])
                assert excerpt_meta["truncated"] and "must reauthenticate immediately" in excerpt_text, excerpt_text
                excerpt = excerpt_meta["text_excerpts"][0]
                assert excerpt["node_id"] == "Memory:shared" and excerpt["total_chars"] == len(long_body)
                page = await call(a, "get_context", {"node_ids": ["Memory:shared"], "text_property": "body",
                                  "text_offset": excerpt["offset"], "text_sha256": excerpt["sha256"], "token_budget": 1500})
                assert "must reauthenticate immediately" in page
                assert json.loads(page.rsplit("\n---\n", 1)[-1])["text_page"]["sha256"] == excerpt["sha256"]
                node = json.loads(await call(a, "cypher_query", {"cypher": "MATCH (n:Memory {id:'shared'}) RETURN n"}))["rows"][0][0]
                assert "embedding" not in node and node["_source"] == str(docs / "0.md")
                if embeddings:
                    await ready(a)
                    projected = json.loads(await call(b, "cypher_query", {"cypher": "MATCH (n:Memory {id:'shared'}) RETURN n.embedding"}))["rows"][0][0]
                    assert projected and isinstance(projected[0], float)

                def edit(body):
                    return {"nodes": [{"label": "Memory", "key": "shared", "source": str(docs / "0.md"), "expected_revision": node["_revision"], "properties": {"body": body}}]}

                edits = await asyncio.gather(call(a, "upsert_nodes", edit("first client edit"), allow_error=True), call(b, "upsert_nodes", edit("second client edit"), allow_error=True))
                assert sum("revision_conflict" in text for text in edits) == 1, edits
                assert sum(not text.startswith("ERROR:") for text in edits) == 1, edits
                stale = await call(b, "get_context", {"node_ids": ["Memory:shared"], "text_property": "body",
                                   "text_sha256": excerpt["sha256"], "text_offset": excerpt["offset"]}, allow_error=True)
                assert "Text changed since the previous page" in stale
                # M19: both harnesses see explicit supersession, guarded review
                # history and historical text through these same tools.
                current = json.loads(await call(a, "cypher_query", {"cypher": "MATCH (n:Memory {id:'shared'}) RETURN n"}))["rows"][0][0]
                retired = json.loads(await call(b, "cypher_query", {"cypher": "MATCH (n:Memory {id:'claude-shaped-0-0'}) RETURN n"}))["rows"][0][0]
                adoption = {"operation_id": "m19:policy", "nodes": [
                    {"label": "Memory", "key": "shared", "expected_revision": current["_revision"],
                     "properties": {"body": "Authorization cache policy allows 15 seconds."}, "source": str(docs / "0.md"),
                     "evidence": {"actor": "Claude Code", "review": "accepted", "reason": "Corrected cache policy"}},
                    {"label": "Memory", "key": "claude-shaped-0-0", "expected_revision": retired["_revision"],
                     "properties": {"body": "Authorization cache policy allowed 30 minutes."}, "source": str(docs / "1.md"),
                     "evidence": {"actor": "Cursor", "state": "superseded", "superseded_by": "Memory:shared"}},
                ]}
                adopted = json.loads(await call(a, "upsert_nodes", adoption))
                await ready(a)
                active = await call(b, "search_knowledge", {"query": "authorization cache policy", "labels": ["Memory"], "top_k": 3, "hops": 1, "token_budget": 3000})
                assert "15 seconds" in active and "30 minutes" not in active
                historical = await call(a, "get_context", {"node_ids": ["Memory:claude-shaped-0-0"], "evidence": "all", "token_budget": 3000})
                assert "30 minutes" in historical and "superseded" in historical
                reviews = [{"nodes": [{"label": "Memory", "key": "shared", "expected_revision": adopted["revisions"]["Memory:shared"],
                            "evidence": {"actor": actor, "review": "accepted", "reason": "Reviewed the corrected source"}}]}
                           for actor in ("Claude Code", "Cursor")]
                outcomes = await asyncio.gather(call(a, "upsert_nodes", reviews[0], allow_error=True), call(b, "upsert_nodes", reviews[1], allow_error=True))
                assert sum("revision_conflict" in value for value in outcomes) == 1
                history = json.loads((await call(b, "get_context", {"node_ids": ["Memory:shared"], "history": True, "token_budget": 3000})).rsplit("\n---\n", 1)[-1])["history"]
                assert [entry["sequence"] for entry in history["entries"]] == [2, 1, 0]
                assert history["entries"][-1]["baseline"] and history["entries"][-1]["actor"] is None
                assert history["entries"][1]["actor"] == "Claude Code"
                snapshot = await call(b, "get_context", {"node_ids": ["Memory:shared"], "revision": 1, "text_property": "body", "token_budget": 1500})
                assert "15 seconds" in snapshot and "_history_revision" in snapshot
            # Disconnecting one agent must leave the owning server available.
            assert json.loads(await call(a, "cypher_query", {"cypher": "MATCH (n:Memory) RETURN count(n)"}))["rows"] == [[20 * batches + 1]]
            await ready(a)
            first_health = await asyncio.to_thread(health)
        await asyncio.to_thread(stop)
        async with connect() as session:
            assert json.loads(await call(session, "cypher_query", {"cypher": "MATCH (n:Memory) RETURN count(n)"}))["rows"] == [[20 * batches + 1]]
            assert json.loads(await call(session, "cypher_query", {"cypher": "MATCH (n:Document) RETURN count(n)"}))["rows"] == [[doc_count]]
            assert json.loads(await call(session, "upsert_nodes", batch("claude-shaped", 0, "updated")))["replayed"]
            assert json.loads(await call(session, "upsert_nodes", adoption))["replayed"]
            history = json.loads((await call(session, "get_context", {"node_ids": ["Memory:shared"], "history": True, "token_budget": 3000})).rsplit("\n---\n", 1)[-1])["history"]
            assert [entry["sequence"] for entry in history["entries"]] == [2, 1, 0]
            await ready(session)
            assert (await asyncio.to_thread(health))["pid"] != first_health["pid"]
        await asyncio.to_thread(stop)
        with Engine(GragConfig(db_path=db, buffer_pool_size=128 * 1024**2), read_only=True) as engine:
            assert not any(index["index_type"] == "HNSW" for index in engine.execute("CALL SHOW_INDEXES() RETURN *").as_dicts())
        report = {"command": command, "runtime_version": first_health["version"], "clients": 2,
                  "real_local_embeddings": embeddings, "memories": 20 * batches + 1, "documents": doc_count,
                  "concurrent_revisions": 20 * batches, "conflict_refused": True, "independent_disconnect": True,
                  "restart_replay": True, "native_hnsw_indexes": 0, "total_ms": (time.perf_counter() - started) * 1000,
                  "typed_inputs": True, "schema_revision_roundtrip": True, "structured_errors": True,
                  "compact_whole_entities": True,
                  "excerpt_paging_and_stale_hash": True,
                  "evidence_history_supersession_review_restart": True,
                  "tool_calls": {name: {"count": len(values), "median_ms": statistics.median(values),
                                        "p95_ms": percentile95(values), "samples_ms": values} for name, values in latencies.items()},
                  "limitations": "Scripted protocol clients, not actual Claude/Cursor UIs or autonomous LLM runs."}
        destination = Path(os.environ.get("GRAG_SHARED_WORKFLOW_REPORT", str(root / "shared-workflow.json")))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        await asyncio.to_thread(stop)
