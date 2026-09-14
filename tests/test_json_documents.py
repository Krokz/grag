"""M35: explicit JSON source ingestion preserves records and collection ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from grag.core.errors import ConfigurationError, ResourceLimitError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    UpsertEdgesRequest,
    UpsertNodesRequest,
)
from grag.ingest.loaders import ingest_documents, load_paths, load_request
from grag.retrieval.context import get_context


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("text", [
    '{"$schema":"https://example.invalid/schema","$ref":"file:///missing.json","description":"cranberry contract"}',
    '[{"text":"a fixture","source":"not-its-owner","metadata":{"n":1}}]',
    '{"documents":[{"text":"a fixture"}]}',
    '{"duplicate":1,"duplicate":2,"integer":' + '9' * 5000 + ',"exponent":1e9000}',
    '[]', '{}', 'true', 'null', '42', '"hello \\u263a"',
])
def test_document_mode_retains_source_text_and_file_provenance(tmp_path, text):
    path = write(tmp_path, "contract.json", text)
    docs, warnings, count = load_paths([path], json_mode="document")
    assert count == 1 and not warnings and len(docs) == 1
    assert docs[0].text == text and docs[0].source == str(path)
    assert docs[0].source_file == str(path)
    assert docs[0].metadata == {
        "format": "json", "coverage": "source_text",
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_document_mode_accepts_utf8_bom_and_escaped_structural_characters(tmp_path):
    text = json.dumps({"description": '[{\\"' * 100, "שלום": "🌍"}, ensure_ascii=False)
    path = write(tmp_path, "windows.JSON", "\ufeff" + text)
    docs, warnings, _ = load_paths([path], json_mode="document")
    assert not warnings and docs[0].text == text
    assert docs[0].metadata["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("text", [
    '', '{"broken":', '{"trailing":1,}', '{"value":NaN}', '[Infinity]', '-Infinity',
    '{} {}', '[' * 65 + '0' + ']' * 65,
])
def test_invalid_document_warns_and_never_authorizes_deletion(tmp_path, text):
    path = write(tmp_path, "broken.json", text)
    req, warnings, count = load_request([tmp_path], json_mode="document")
    assert count == 0 and req.documents == [] and req.sync_paths == []
    assert any(str(path) in w and "could not load" in w for w in warnings)
    assert any("synchronization was skipped" in w for w in warnings)


def test_nesting_boundary_and_invalid_encoding(tmp_path):
    path = write(tmp_path, "nested.json", '[' * 64 + '0' + ']' * 64)
    assert load_paths([path], json_mode="document")[2] == 1
    path.write_bytes(b'{"invalid":"\xff"}')
    req, warnings, _ = load_request([path], json_mode="document")
    assert not req.documents and not req.sync_paths and warnings


def test_records_remain_default_and_jsonl_remains_records(tmp_path):
    rows = [{"text": "one", "source": "https://example.invalid/one", "metadata": {"v": 1}},
            {"text": "two"}]
    path = write(tmp_path, "batch.json", json.dumps({"documents": rows}))
    default = load_paths([path])
    assert default == load_paths([path], json_mode="records")
    assert [d.text for d in default[0]] == ["one", "two"]
    assert default[0][0].source == rows[0]["source"] and default[0][0].metadata == {"v": 1}
    line = write(tmp_path, "batch.jsonl", '\n'.join(json.dumps(row) for row in rows))
    assert load_paths([line], json_mode="document") == load_paths([line])
    schema = write(tmp_path, "schema.json", '{"type":"object"}')
    req, warnings, _ = load_request([schema])
    assert not req.documents and not req.sync_paths
    assert any("--json-mode document" in w for w in warnings)


def test_invalid_mode_is_rejected_before_reading_sources(tmp_path):
    with pytest.raises(ConfigurationError, match="json_mode"):
        load_request([tmp_path], json_mode="guess")


def test_source_limits_abort_the_whole_request(tmp_path):
    path = write(tmp_path, "large.json", '"' + 'x' * (2 * 1024 * 1024) + '"')
    with pytest.raises(ResourceLimitError) as error:
        load_request([path], json_mode="document")
    assert "Split a large document" in error.value.hint and "max_file_kb" not in error.value.hint
    path.unlink()
    for name in ("one.json", "two.json"):
        write(tmp_path, name, '"' + 'x' * (1024 * 1024) + '"')
    with pytest.raises(ResourceLimitError):
        load_request([tmp_path], json_mode="document")


def test_ignore_and_repository_boundaries_still_apply(tmp_path):
    root = tmp_path / "repo"
    visible = write(root, "visible.json", '{}')
    ignored = write(root, "ignored.json", '{}')
    write(root, ".gitignore", 'ignored.json\n')
    write(root, "nested/.git", 'gitdir: /irrelevant\n')
    write(root, "nested/hidden.json", '{}')
    write(root, "plain.md", '# unchanged\n')
    docs, _, count = load_paths([root, ignored], json_mode="document")
    assert count == 2 and {d.source for d in docs} == {str(visible), str(root / "plain.md")}


@pytest.mark.parametrize("sections", [False, True])
def test_source_sync_mode_changes_and_authored_links(engine, tmp_path, sections):
    root = tmp_path / "docs"
    schema = write(root, "schema.json", '{"description":"cranberry oldcontract"}')
    removed = write(root, "removed.json", '{"description":"removedcontract"}')

    def sync(paths=None, mode="document", label="Chunk"):
        req, warnings, _ = load_request(paths or [root], sections=sections, json_mode=mode, label=label)
        result = ingest_documents(engine, engine.config, req)
        return result, warnings

    sync()
    first = engine.execute("MATCH (n:Chunk) RETURN n.id,n._source ORDER BY n._source").rows
    sync()
    assert engine.execute("MATCH (n:Chunk) RETURN n.id,n._source ORDER BY n._source").rows == first
    removed_id = next(key for key, source in first if source == str(removed))
    define_schema(engine, engine.config, DefineSchemaRequest.model_validate({
        "node_tables": [{"name": "Memory", "properties": [{"name": "text"}]}],
        "rel_tables": [{"name": "CITES", "from_label": "Memory", "to_label": "Chunk"}],
    }))
    upsert_nodes(engine, engine.config, UpsertNodesRequest.model_validate({
        "nodes": [{"label": "Memory", "key": "authored", "properties": {"text": "Keep this evidence."}}],
    }))
    upsert_edges(engine, engine.config, UpsertEdgesRequest.model_validate({"edges": [{
        "type": "CITES", "from_label": "Memory", "from_key": "authored", "to_label": "Chunk", "to_key": removed_id,
    }]}))
    removed.unlink()
    schema.write_text('{"broken":', encoding="utf-8")
    _, warnings = sync()
    assert warnings and engine.execute("MATCH (n:Chunk) RETURN count(n)").rows == [[2]]
    schema.write_text('{"description":"cranberry newcontract"}', encoding="utf-8")
    result, warnings = sync()
    assert not warnings and any("obsolete" in w for w in result.warnings)
    assert engine.execute("MATCH (:Memory)-[:CITES]->(n:Chunk) RETURN n._document_state").rows == [["obsolete"]]
    assert engine.execute("MATCH (n:Memory) RETURN n.text").rows == [["Keep this evidence."]]
    context = get_context(engine, engine.config, ContextRequest(node_ids=[f"Chunk:{removed_id}"], hops=0))
    assert not context.included_node_ids and context.excluded_evidence == 1
    # A successful explicit mode switch reconciles the old file-owned document,
    # even when the replacement records use URL provenance or no source.
    schema.write_text(json.dumps([{"text": "record replacement", "source": "https://example.invalid/new"}, {"text": "anonymous"}]), encoding="utf-8")
    sync([schema], mode="records", label="OtherChunk")
    assert engine.execute("MATCH (n:Chunk) WHERE n._document_state='current' RETURN n.id").rows == []
    assert engine.execute("MATCH (n:OtherChunk) RETURN count(n)").rows == [[2]]
    sync([schema], label="OtherChunk")
    assert engine.execute("MATCH (n:OtherChunk) RETURN count(n)").rows == [[1]]
    schema.write_text('[]', encoding="utf-8")
    sync([schema], label="OtherChunk")
    assert engine.execute("MATCH (n:OtherChunk) RETURN n.text").rows == [['[]']]
    sync([schema], mode="records", label="OtherChunk")
    assert engine.execute("MATCH (n:OtherChunk) RETURN count(n)").rows == [[0]]


def test_loaded_json_uses_existing_rest_ingest_contract(tmp_path):
    from grag.api.main import create_app
    from grag.config import GragConfig

    path = write(tmp_path, "schema.json", '{"description":"cranberry contract"}')
    req, warnings, _ = load_request([path], json_mode="document", sections=True)
    assert not warnings
    config = GragConfig(db_path=tmp_path / "rest.lbdb", buffer_pool_size=128 * 1024**2)
    with TestClient(create_app(config)) as client:
        result = client.post("/api/ingest", json=req.model_dump())
        assert result.status_code == 200 and result.json()["documents"] == 1
        rows = client.post("/api/query", json={"cypher": "MATCH (c:Chunk) RETURN c.text,c.meta,c._source"}).json()["rows"]
        assert rows[0][0] == path.read_text() and rows[0][2] == str(path)
        assert json.loads(rows[0][1])["coverage"] == "source_text"


def test_source_mcp_background_json_and_cli_reingest_survive_restart(tmp_path):
    asyncio.run(_mcp_workflow(tmp_path))


async def _mcp_workflow(root):
    path = write(root, "schema.json", '{"description":"cranberry schema","$ref":"https://example.invalid/never-fetch"}')
    db = root / "workflow.lbdb"
    launcher = os.environ.get("GRAG_TEST_COMMAND")
    command = [launcher] if launcher else [sys.executable, "-m", "grag.cli"]
    env = {**get_default_environment(), "PATH": os.environ.get("PATH", ""),
           "GRAG_EMBED_PROVIDER": "", "GRAG_AUTO_REFRESH_CODE": "0", "GRAG_BUFFER_POOL_MB": "128"}
    if not launcher:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    if os.environ.get("LBUG_PYTHON_BACKEND") == "capi":
        env["LBUG_PYTHON_BACKEND"] = "capi"
    params = StdioServerParameters(command=command[0], args=[*command[1:], "--db", str(db), "mcp"], env=env, cwd=str(root))

    @asynccontextmanager
    async def connect():
        with (root / "mcp.stderr").open("a") as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=60) as session:
                    await session.initialize()
                    yield session

    async def call(session, name, args):
        result = await session.call_tool(name, args)
        text = '\n'.join(part.text for part in result.content if part.type == "text")
        assert not result.is_error, text
        return text, json.loads(text.rsplit('\n---\n', 1)[-1])

    async with connect() as session:
        tools = (await session.list_tools()).tools
        assert len(tools) == 10
        tool = next(t for t in tools if t.name == "ingest_docs")
        assert tool.input_schema["properties"]["json_mode"]["enum"] == ["records", "document"]
        assert tool.input_schema["properties"]["json_mode"]["default"] == "records"
        _, skipped = await call(session, "ingest_docs", {"paths": [str(path)]})
        assert skipped["files_read"] == 0 and skipped["warnings"]
        _, job = await call(session, "ingest_docs", {"paths": [str(path)], "json_mode": "document", "background": True})
        assert job["json_mode"] == "document" and job["files_read"] == 1 and not job["warnings"]
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            _, done = await call(session, "job_status", {"job_id": job["id"]})
            if done["status"] not in {"queued", "running"}:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert done["status"] == "done" and not done["result"]["warnings"]
        await call(session, "describe_schema", {})
        _, before = await call(session, "cypher_query", {"cypher": "MATCH (c:Chunk) RETURN c.id,c.text,c._source,c.meta"})
        assert len(before["rows"]) == 1 and before["rows"][0][1] == path.read_text()
        assert before["rows"][0][2] == str(path)
        found, meta = await call(session, "search_knowledge", {"query": "cranberry schema", "labels": ["Chunk"], "hops": 0, "token_budget": 1200})
        assert "cranberry" in found and str(path) in found
        assert meta["response_token_estimate"] <= 1200

    path.write_text('{"description":"cranberry revised"}', encoding="utf-8")
    result = subprocess.run([*command, "--db", str(db), "ingest", "--sections", "--json-mode", "document", str(path)],  # noqa: S603 -- selected test launcher, temporary inputs
                            cwd=root, env=env, text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert "1 document(s) from 1 file(s)" in result.stdout and "JSON mode: document" in result.stdout
    async with connect() as session:
        await call(session, "describe_schema", {})
        _, after = await call(session, "cypher_query", {"cypher": "MATCH (c:Chunk) RETURN c.id,c.text,c._source,c.meta"})
        assert len(after["rows"]) == 1 and after["rows"][0][0] == before["rows"][0][0]
        assert after["rows"][0][1] == path.read_text()
        assert json.loads(after["rows"][0][3])["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
