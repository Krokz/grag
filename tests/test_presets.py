"""M18: the opt-in memory preset is versioned, additive and reports conflicts."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grag import cli
from grag.admin import ServerInfo
from grag.api.main import create_app
from grag.client import GraphClient
from grag.config import GragConfig
from grag.core import presets
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError, SchemaError
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.schema import build_schema_document, schema_text
from grag.core.types import (
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval.search import search_knowledge

PRESET = DefineSchemaRequest(preset="memory")


def _columns(engine, table):
    return {row[1]: row[2] for row in engine.execute(f"CALL TABLE_INFO('{table}') RETURN *").rows}


def _version(engine):
    return engine.execute("MATCH (m:_grag_meta {key: 'preset.memory.version'}) RETURN m.value").rows


def _search(engine, query):
    return [s.node.id for s in search_knowledge(
        engine, engine.config, SearchRequest(query=query, labels=["Decision"], hops=0)).seeds]


def test_fresh_adoption_creates_tables_records_version_and_repeats_cleanly(engine):
    doc = define_schema(engine, engine.config, PRESET)
    assert doc.preset.created == ["Decision", "Insight", "Task", "Question"]
    assert doc.preset.previous_version is None and not doc.preset.conflicts
    for label in doc.preset.created:
        assert _columns(engine, label).items() >= {"id": "STRING", "title": "STRING", "body": "STRING",
                                                    "status": "STRING", "scope": "STRING"}.items()
    assert _version(engine) == [["1"]]
    again = define_schema(engine, engine.config, PRESET)
    assert again.preset.previous_version == 1
    assert not (again.preset.created or again.preset.added or again.preset.conflicts)
    # The report is not part of the schema view or its revision.
    plain = build_schema_document(engine, engine.config)
    assert plain.preset is None and plain.schema_revision == again.schema_revision
    assert '"preset"' in schema_text(again) and '"preset"' not in schema_text(plain)


def test_existing_table_gains_properties_and_its_search_index_covers_them(engine, tmp_path):
    define_schema(engine, engine.config, DefineSchemaRequest(
        node_tables=[NodeTableSpec(name="Decision", properties=[PropertySpec(name="body")])]))
    upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(
        label="Decision", key="cache", properties={"body": "alpha body"})]))
    assert _search(engine, "alpha") == ["Decision:cache"]  # builds the index over (body)

    report = define_schema(engine, engine.config, PRESET).preset
    assert report.added == ["Decision.title", "Decision.status", "Decision.scope"]
    assert report.created == ["Insight", "Task", "Question"]
    result = upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(
        label="Decision", key="cache", properties={"title": "zebra heading"})]))
    assert not result.warnings
    assert _search(engine, "zebra") == ["Decision:cache"]
    engine.close()
    reopened = Engine(engine.config)
    try:
        assert _search(reopened, "zebra") == ["Decision:cache"]
        assert _search(reopened, "alpha") == ["Decision:cache"]
    finally:
        reopened.close()


def test_conflicts_are_reported_and_left_unchanged_on_every_run(engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[
        NodeTableSpec(name="Decisions", properties=[PropertySpec(name="body")]),
        NodeTableSpec(name="Task", properties=[PropertySpec(name="status", type="INT64")]),
        NodeTableSpec(name="Question", primary_key="slug"),
    ], rel_tables=[RelTableSpec(name="Insight", from_label="Task", to_label="Task")]))
    before = {t: _columns(engine, t) for t in ("Decisions", "Task")}
    added = ["Task.title", "Task.body", "Task.scope", "Question.title", "Question.body", "Question.status", "Question.scope"]
    for expected in (added, []):
        report = define_schema(engine, engine.config, PRESET).preset
        assert report.created == [] and report.added == expected
        conflicts = "\n".join(report.conflicts)
        assert "Decision not created: similar table Decisions" in conflicts
        assert "Insight exists as a relationship table" in conflicts
        assert "Task.status is INT64" in conflicts
        assert "Question key is slug:STRING" in conflicts
    tables = {row[1] for row in engine.execute("CALL SHOW_TABLES() RETURN *").rows}
    assert "Decision" not in tables
    assert _columns(engine, "Decisions") == before["Decisions"]
    assert _columns(engine, "Task")["status"] == "INT64"
    assert _version(engine) == [["1"]]
    # allow_similar creates the preset table beside the near-duplicate.
    assert define_schema(engine, engine.config, DefineSchemaRequest(preset="memory", allow_similar=True)).preset.created == ["Decision"]


def test_newer_recorded_version_is_refused_without_changes(engine):
    engine.execute_write("MERGE (m:_grag_meta {key: 'preset.memory.version'}) ON CREATE SET m.value = '2' ON MATCH SET m.value = '2'")
    with pytest.raises(SchemaError, match="supports up to 1"):
        define_schema(engine, engine.config, PRESET)
    assert "Decision" not in {row[1] for row in engine.execute("CALL SHOW_TABLES() RETURN *").rows}


def test_migration_steps_run_once_when_crossing_their_version(engine, monkeypatch):
    define_schema(engine, engine.config, PRESET)
    calls = []
    monkeypatch.setitem(presets.PRESETS, "memory", (2, presets.PRESETS["memory"][1]))
    monkeypatch.setitem(presets.MIGRATIONS, "memory", {
        1: lambda engine, report: calls.append(1), 2: lambda engine, report: calls.append(2)})
    report = define_schema(engine, engine.config, PRESET).preset
    assert (report.previous_version, report.version, calls) == (1, 2, [2])
    define_schema(engine, engine.config, PRESET)
    assert calls == [2] and _version(engine) == [["2"]]


def test_preset_is_its_own_call():
    with pytest.raises(ValidationError, match="separate call"):
        DefineSchemaRequest(preset="memory", node_tables=[NodeTableSpec(name="Note")])


def test_mcp_preset_needs_no_table_lists(tmp_path):
    from grag.mcp_server import server as mcp
    from grag.service import GragService

    svc = GragService(GragConfig(db_path=tmp_path / "mcp.lbdb", buffer_pool_size=128 * 1024**2))
    try:
        text = mcp.define_schema(svc, preset="memory")
        assert "Decision(" in text and '"preset":{"name":"memory","version":1' in text
    finally:
        svc.close()


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAG_EMBED_PROVIDER", "")
    monkeypatch.setenv("GRAG_AUTO_REFRESH_CODE", "0")
    monkeypatch.setattr("grag.admin.find_server", lambda db: None)
    config = GragConfig(db_path=tmp_path / "memory.lbdb", buffer_pool_size=128 * 1024**2)

    def run(*args, success=True):
        status = cli.main(["--db", str(config.db_path), *args])
        output = capsys.readouterr()
        assert status == (0 if success else 1), output
        return json.loads(output.out) if success and "--json" in args else output

    run.config = config
    return run


def test_cli_adopt_json_and_human_output(run):
    assert run("memory", "adopt", "--json")["created"] == ["Decision", "Insight", "Task", "Question"]
    out = run("memory", "adopt").out
    assert "Memory preset v1" in out and "No changes needed." in out


def test_client_refuses_an_owner_without_preset_support(run, monkeypatch):
    with TestClient(create_app(run.config)) as http:
        monkeypatch.setattr("grag.admin.find_server", lambda db: ServerInfo(8471, "0.12.0", True, None))

        class OldOwner:
            def request(self, method, path, **kwargs):
                response = http.request(method, path, **kwargs)
                if path == "/api/health":
                    body = response.json()
                    body["capabilities"].pop("memory_preset")
                    return type("Health", (), {"status_code": 200, "json": lambda self: body})()
                if path == "/api/schema/define":
                    pytest.fail("sent a preset request to an owner without support")
                return response

            def close(self):
                pass

        monkeypatch.setattr("grag.client.httpx2.Client", lambda **kw: OldOwner())
        with GraphClient(run.config) as client, pytest.raises(ConfigurationError, match="schema presets"):
            client.call("define_schema", PRESET)


def test_init_adopts_the_preset_and_dry_run_opens_no_database(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    for key in ("GRAG_DB_PATH", "GRAG_DB_DIR", "GRAG_SERVER_URL", "GRAG_EMBED_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GRAG_AUTO_REFRESH_CODE", "0")
    monkeypatch.setenv("GRAG_BUFFER_POOL_MB", "128")
    monkeypatch.setattr("grag.admin.find_server", lambda db: None)
    monkeypatch.chdir(root)
    db = tmp_path / "project.lbdb"
    args = ["--db", str(db), "init", "--client", "claude", "--no-verify", "--memory-preset"]
    assert cli.main([*args, "--dry-run"]) == 0
    assert "Would adopt the memory preset" in capsys.readouterr().out and not db.exists()
    assert cli.main(args) == 0
    assert "Created: Decision, Insight, Task, Question" in capsys.readouterr().out
    assert cli.main([*args[:3], "--remove", "--memory-preset"]) == 1
