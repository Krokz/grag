"""First-use mapping only enrolls an empty or verified setup-only graph."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grag.cli import main
from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.errors import ConfigurationError
from grag.core.types import DefineSchemaRequest, QueryRequest, UpsertNodesRequest
from grag.onboarding import SETUP_SOURCE, _initial_mapping_needed, ingest_if_empty


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "main.py").write_text("def greet():\n    return 'hello'\n")
    for key in ("GRAG_DB_PATH", "GRAG_DB_DIR", "GRAG_SERVER_URL", "GRAG_EMBED_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GRAG_AUTO_REFRESH_CODE", "0")
    monkeypatch.setenv("GRAG_BUFFER_POOL_MB", "128")
    monkeypatch.setattr("grag.admin.find_server", lambda db: None)
    monkeypatch.chdir(root)
    return root, GragConfig(db_path=tmp_path / "project.lbdb", buffer_pool_size=128 * 1024 * 1024, auto_refresh_code=False)


def seed(config, label="GragSetup", key="connection", source=SETUP_SOURCE):
    with GraphClient(config) as client:
        client.call("define_schema", DefineSchemaRequest.model_validate({"node_tables": [{
            "name": label, "primary_key": "id", "searchable": False,
            "properties": [{"name": "nonce"}],
        }]}))
        client.call("upsert_nodes", UpsertNodesRequest(nodes=[{
            "label": label, "key": key, "properties": {"nonce": "verified-nonce"}, "source": source,
        }]))


@pytest.mark.parametrize("setup", [False, True])
def test_initial_mapping_and_repeat_preserve_existing_scope(project, setup):
    root, config = project
    if setup:
        seed(config)
    result = ingest_if_empty(config, root)
    assert result["status"] == "ingested"
    assert result["result"]["modules"] == 1
    with GraphClient(config) as client:
        assert client.call("cypher_query", QueryRequest(
            cypher="MATCH (f:Function) RETURN f.name,f.line_start",
        ))["rows"] == [["greet", 1]]
    # A subsequent invocation must not silently widen the saved scope or refresh.
    (root / "later.py").write_text("def later():\n    pass\n")
    assert ingest_if_empty(config, root)["status"] == "skipped"
    with GraphClient(config) as client:
        assert client.call("cypher_query", QueryRequest(
            cypher="MATCH (m:Module) RETURN count(m)",
        ))["rows"] == [[1]]


@pytest.mark.parametrize("label,key,source", [
    ("Memory", "decision", "user"),
    ("Document", "guide", "user"),
    ("GragSetup", "connection", "authored context"),
    ("GragSetup", "another", SETUP_SOURCE),
])
def test_existing_content_blocks_even_before_init_verification(project, monkeypatch, capsys, label, key, source):
    root, config = project
    seed(config, label, key, source)
    monkeypatch.setattr("grag.onboarding.verify_registrations", lambda *args: pytest.fail("verification would overwrite the marker"))
    assert main(["--db", str(config.db_path), "init", "--client", "claude", "--ingest-if-empty"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "skipped"
    assert not (root / ".mcp.json").exists()
    assert not (root / ".grag/project.json").exists()
    with GraphClient(config) as client:
        assert client.call("cypher_query", QueryRequest(
            cypher=f"MATCH (n:{label}) RETURN n.id,n._source",
        ))["rows"] == [[key, source]]


@pytest.mark.parametrize("schema", [
    {"detail": "compact", "node_tables": [], "rel_tables": []},
    {"detail": "full", "unchanged": True, "node_tables": [], "rel_tables": []},
    {"detail": "full", "node_tables": [{"row_count": None}], "rel_tables": []},
    {"detail": "full", "node_tables": [], "rel_tables": [{"row_count": -1}]},
    {"detail": "full", "node_tables": [{"row_count": False}], "rel_tables": []},
])
def test_incomplete_schema_never_means_empty(schema):
    client = SimpleNamespace(call=lambda operation: schema)
    with pytest.raises(ConfigurationError, match="whether the graph is empty"):
        _initial_mapping_needed(client)


def test_empty_tables_allowed_but_relationship_content_blocks():
    schema = {"detail": "full", "node_tables": [{"name": "Memory", "row_count": 0}], "rel_tables": []}
    client = SimpleNamespace(call=lambda operation: schema)
    assert _initial_mapping_needed(client)
    schema["rel_tables"] = [{"row_count": 1}]
    assert not _initial_mapping_needed(client)


def test_zero_supported_files_is_reported_and_ignores_are_respected(project):
    root, config = project
    (root / ".gitignore").write_text("*\n")
    result = ingest_if_empty(config, root)
    assert result["status"] == "no_supported_code"
    assert result["result"]["modules"] == 0


def test_cli_maps_resolved_root_without_mcp_and_is_repeatable(project, monkeypatch, capsys):
    root, config = project
    # Keep configuration backups local without replacing the native extension cache.
    monkeypatch.setattr(Path, "home", lambda: root.parent / "home")
    child = root / "src"
    child.mkdir()
    monkeypatch.chdir(child)
    args = ["--db", str(config.db_path), "init", "--client", "codex", "--no-mcp", "--no-claude-md", "--no-skill", "--ingest-if-empty"]
    assert main([*args, "--dry-run"]) == 0
    assert not config.db_path.exists()
    assert not (root / ".grag").exists()
    assert main(args) == 0
    assert '"status": "ingested"' in capsys.readouterr().out
    assert (root / ".grag/project.json").exists()
    assert not (child / ".grag").exists()
    before = (root / ".grag/project.json").read_bytes()
    assert main(args) == 0
    assert '"status": "skipped"' in capsys.readouterr().out
    assert (root / ".grag/project.json").read_bytes() == before


@pytest.mark.parametrize("extra", [["--remove"], ["--server-url", "http://127.0.0.1:1234"]])
def test_initial_mapping_refuses_incompatible_modes(project, capsys, extra):
    root, config = project
    assert main(["--db", str(config.db_path), "init", "--ingest-if-empty", *extra]) == 1
    assert "local checkout database" in capsys.readouterr().err
    assert not config.db_path.exists()
    assert not (root / ".grag").exists()


def test_remote_environment_never_gets_current_checkout(project, monkeypatch, capsys):
    root, config = project
    monkeypatch.setenv("GRAG_SERVER_URL", "http://127.0.0.1:1234")
    assert main(["init", "--ingest-if-empty"]) == 1
    assert "local checkout database" in capsys.readouterr().err
    assert not config.db_path.exists()
    assert not (root / ".grag").exists()


@pytest.mark.parametrize("error", [ConfigurationError("Owner identity changed"), RuntimeError("Corrupted wal file")])
def test_failed_state_check_does_not_initialize_a_replacement(project, monkeypatch, capsys, error):
    root, config = project
    config.db_path.write_bytes(b"original fixture")

    def fail(config):
        raise error

    monkeypatch.setattr("grag.onboarding.initial_mapping_needed", fail)
    assert main(["--db", str(config.db_path), "init", "--client", "claude", "--ingest-if-empty"]) == 1
    assert str(error) in capsys.readouterr().err
    assert config.db_path.read_bytes() == b"original fixture"
    assert not (root / ".mcp.json").exists()
    assert not (root / ".grag").exists()
