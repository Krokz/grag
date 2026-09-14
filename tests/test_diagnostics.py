"""Discover without changing files; verify the actual saved launcher on opt-in."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from grag import admin, cli, diagnostics, onboarding, readiness
from grag.config import GragConfig


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(admin, "run_dir", lambda: home / ".grag/run")
    monkeypatch.setattr(admin, "log_dir", lambda: home / ".grag/logs")
    for key in list(os.environ):
        if key.startswith("GRAG_") or key == "CLAUDE_CONFIG_DIR":
            monkeypatch.delenv(key)
    monkeypatch.setattr(admin, "probe_health", lambda *a, **kw: None)
    return root, home


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def saved(root, *, name="grag", db=None, **extra):
    entry = {"command": sys.executable, "args": ["-m", "grag.cli", "--db", str(db or root / "knowledge.lbdb"), "mcp"], **extra}
    write(root / ".mcp.json", {"mcpServers": {name: entry}, "unrelated": {"secret": "keep me"}})
    return entry


def collect(db=None):
    return diagnostics.collect(argparse.Namespace(cmd="status", db=str(db) if db else None, db_dir=None))


def test_passive_discovery_never_opens_corrupt_db_or_cleans_registration(workspace, monkeypatch):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    saved(root)
    for suffix in ("", ".wal", ".shadow"):
        Path(str(db) + suffix).write_bytes(b"corrupt")
    admin.write_pidfile(db, 41001)
    monkeypatch.setattr(admin, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(onboarding, "verify_entry", lambda *a, **kw: pytest.fail("launched client"))
    monkeypatch.setattr("grag.core.engine.Engine.__init__", lambda *a, **kw: pytest.fail("opened graph"))
    paths = [root / ".mcp.json", db, Path(str(db) + ".wal"), Path(str(db) + ".shadow"), admin.pidfile_path(db)]
    before = {p: p.read_bytes() for p in paths}
    report, _, _ = collect()
    assert report["client_readiness"] == "unverified"
    assert report["registrations"][0]["database"] == str(db)
    assert {p: p.read_bytes() for p in paths} == before


@pytest.mark.parametrize("content", ["{oops", '{"mcpServers": {"grag": 3}}', '{"mcpServers": []}'])
def test_bad_registration_does_not_prevent_json_diagnosis(workspace, capsys, content):
    root, _ = workspace
    (root / ".mcp.json").write_text(content)
    assert cli.main(["status", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["project"]["error"]
    assert report["runtime"]["python"] == sys.executable


def test_moved_mapping_and_other_client_registration_are_both_reported(workspace):
    from uuid import uuid4

    root, _ = workspace
    saved(root, name="grag-algo3")
    write(root / ".grag/project.json", {"version": 1, "checkout_id": str(uuid4()), "root": str(root / "old"), "db_path": str(root / "db.lbdb"), "port": 41001})
    report, cfg, _ = collect()
    assert cfg is None
    assert "relocate" in report["project"]["repair"]
    assert report["registrations"][0]["id"] == "claude:project:grag-algo3"


def test_scopes_profile_env_and_renamed_servers(workspace, monkeypatch):
    root, home = workspace
    db = root / "existing.lbdb"
    db.touch()
    entry = saved(root, db=db)
    custom = home / "claude-work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    write(custom / ".claude.json", {"mcpServers": {"grag": entry}, "projects": {str(root): {"mcpServers": {"grag": entry}}}, "oauthAccount": {"token": "never show"}})
    write(home / ".cursor/mcp.json", {"mcpServers": {"grag-algo3": entry}})
    write(home / ".config/zed/settings.json", {"context_servers": {"code-graph": {"command": {"path": sys.executable, "args": entry["args"]}}}})
    report, _, _ = collect(db)
    records = {r["id"]: r for r in report["registrations"]}
    assert records["claude:project:grag"]["scope_precedence"] == "shadowed by claude:local:grag"
    assert records["claude:local:grag"]["scope_precedence"] == "highest discovered"
    assert records["cursor:user:grag-algo3"]["database"] == str(db)
    assert records["zed:user:code-graph"]["resolved_command"]
    assert "never show" not in json.dumps(report)


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_linked_configuration_is_inspected_and_preserved(workspace, link):
    root, _ = workspace
    source = root / "shared.json"
    write(source, {"mcpServers": {"grag": {"command": sys.executable, "args": ["--db", str(root / "x.lbdb"), "mcp"]}}})
    try:
        (root / ".mcp.json").symlink_to(source) if link == "symbolic" else os.link(source, root / ".mcp.json")
    except OSError:
        pytest.skip("links unavailable")
    before = source.read_bytes()
    report, _, _ = collect(root / "x.lbdb")
    assert any("linked_configuration" in i for i in report["registrations"][0]["issues"])
    assert source.read_bytes() == before
    assert (root / ".mcp.json").samefile(source)


def test_missing_launcher_wrong_database_and_missing_env(workspace, monkeypatch):
    root, _ = workspace
    saved(root, command=str(root / "old-folder/grag"))
    report, _, _ = collect(root / "other.lbdb")
    issues = " ".join(report["registrations"][0]["issues"])
    assert all(name in issues for name in ("launcher_missing", "database_missing", "database_mismatch"))
    saved(root, env={"GRAG_API_TOKEN": "${M25_TEST_MISSING}"})
    monkeypatch.delenv("M25_TEST_MISSING", raising=False)
    report, _, _ = collect(root / "other.lbdb")
    assert "missing environment variable M25_TEST_MISSING" in report["registrations"][0]["issues"][0]


def test_report_omits_credentials_in_args_headers_environment_and_urls(workspace, monkeypatch):
    root, home = workspace
    monkeypatch.setenv("GRAG_API_TOKEN", "process-secret")
    saved(root, env={"PRIVATE_TOKEN": "saved-secret", "GRAG_DB_PATH": str(root / "db.lbdb")}, args=["--db", str(root / "db.lbdb"), "mcp", "--opaque", "argument-secret"])
    write(home / ".cursor/mcp.json", {"mcpServers": {"grag": {"url": "https://user:password@example.com/mcp?token=url-secret", "headers": {"Authorization": "Bearer header-secret"}}}})
    report, _, private = collect(root / "db.lbdb")
    text = json.dumps(report)
    assert not any(v in text for v in ("saved-secret", "process-secret", "argument-secret", "header-secret", "url-secret", "user:password"))
    assert report["registrations"][0]["database"] == str(root / "db.lbdb")
    assert private["entries"]


@pytest.mark.parametrize("problem", ["missing", "mismatch", "implicit", "url"])
def test_verification_refuses_unproven_target_without_launching(workspace, monkeypatch, problem):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    if problem != "missing":
        db.touch()
    if problem == "implicit":
        saved(root, args=["-m", "grag.cli", "mcp"])
    elif problem == "url":
        write(root / ".mcp.json", {"mcpServers": {"grag": {"url": "http://localhost:1234/mcp"}}})
    else:
        saved(root)
    report, _, private = collect(db if problem != "mismatch" else root / "other.lbdb")
    monkeypatch.setattr(onboarding, "verify_entry", lambda *a, **kw: pytest.fail("must not launch"))
    assert diagnostics.verify(report, private, "claude:project:grag", 1)["status"] == "failed"


@pytest.mark.parametrize("message,expected", [
    ("Corrupted wal file. Read out invalid WAL record type.", "separate copy"),
    ("Buffer manager exception: failed to allocate memory", "not evidence of invalid Cypher"),
    ("database locked", "shared owner"),
])
def test_verification_preserves_startup_cause_and_redacts_logs(workspace, monkeypatch, message, expected):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    db.touch()
    saved(root, env={"ACCESS_TOKEN": "sensitive-value"})
    report, _, private = collect(db)

    async def fail(entry, cwd, **kwargs):
        assert kwargs["read_only"] is True
        kwargs["errlog"].write(message + " sensitive-value")
        raise RuntimeError("connection closed")

    monkeypatch.setattr(onboarding, "verify_entry", fail)
    result = diagnostics.verify(report, private, "claude:project:grag", 1)
    assert message in result["detail"]
    assert expected in result["hint"]
    assert "sensitive-value" not in json.dumps(result)


def test_timeout_is_client_failure_not_install_failure(workspace, monkeypatch, capsys):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    db.touch()
    saved(root)

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(onboarding, "verify_entry", hang)
    monkeypatch.setattr(readiness, "check_install", lambda *a, **kw: [{"status": "ready", "required": True}])
    assert cli.main(["--db", str(db), "doctor", "--json", "--verify-client", "claude:project:grag", "--timeout", "0.01"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert result["ready_scope"] == "installation only"
    assert result["client_readiness"] == "failed"


def test_actual_saved_mcp_read_preserves_authored_memory_and_has_no_setup_write(workspace):
    from grag.client import GraphClient
    from grag.core.types import QueryRequest

    root, _ = workspace
    db = root / "knowledge.lbdb"
    assert cli.main(["--db", str(db), "remember", "Preserve this authored choice", "--id", "choice"]) == 0
    saved(root, env={"GRAG_EMBED_PROVIDER": "", "GRAG_AUTO_REFRESH_CODE": "0", "GRAG_BUFFER_POOL_SIZE": "134217728"})
    report, _, private = collect(db)
    result = diagnostics.verify(report, private, "claude:project:grag", 30)
    assert result["status"] == "verified", result
    assert result["graph_read"] == "verified"
    from grag import __version__

    assert result["reported_server_version"] == __version__
    with GraphClient(GragConfig(db_path=db, buffer_pool_size=128 * 1024 * 1024)) as client:
        schema = client.call("describe_schema")
        assert "GragSetup" not in [t["name"] for t in schema["node_tables"]]
        assert client.call("cypher_query", QueryRequest(cypher="MATCH (n:Memory) RETURN n.text"))["rows"] == [["Preserve this authored choice"]]


def test_passive_index_status_never_opens_an_unloaded_database_or_refreshes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from grag.api.main import create_app
    from grag.core.engine import Engine

    first, second = tmp_path / "first.lbdb", tmp_path / "second.lbdb"
    for db in (first, second):
        with Engine(GragConfig(db_path=db, buffer_pool_size=128 * 1024 * 1024)):
            pass
    app = create_app(GragConfig(db_dir=tmp_path, db_path=first, buffer_pool_size=128 * 1024 * 1024))
    with TestClient(app) as http:
        service = app.state.registry.get("first")
        monkeypatch.setattr(service, "read_freshness", lambda *a, **kw: pytest.fail("refresh scheduled"))
        monkeypatch.setattr(service, "refresh_status", lambda **kw: {"roots": [{"path": "/old/checkout"}], "running": False})
        monkeypatch.setattr(Engine, "__init__", lambda *a, **kw: pytest.fail("opened unloaded database"))
        assert http.get("/api/health").json()["capabilities"]["passive_diagnostics"] == 1
        observed = http.get("/api/index/status?db=first&check=false").json()
        assert observed["index"]["roots"] == [{"path": "/old/checkout"}]
        assert Path(observed["runtime"]["grag_module"]).parts[-2:] == ("grag", "__init__.py")
        second_result = http.get("/api/index/status?db=second&check=false").json()
        assert second_result["loaded"] is False
        assert second_result["database_id"] is None


def test_old_owner_is_not_sent_a_passive_flag_it_would_ignore(workspace, monkeypatch):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    monkeypatch.setattr(admin, "status_lines", lambda cfg: [])
    monkeypatch.setattr(admin, "find_server", lambda target: admin.ServerInfo(1234, "0.9.0", True, 999))
    monkeypatch.setattr(admin, "probe_health", lambda *a, **kw: {"capabilities": {"snapshot_format": 2}})
    monkeypatch.setattr(admin._DIRECT_HTTP, "open", lambda *a, **kw: pytest.fail("old owner could refresh"))
    report = diagnostics._owner(GragConfig(db_path=db))
    assert "lacks passive diagnostics" in report["index"]


def test_existing_empty_database_can_be_verified_without_setup_nodes(workspace):
    from grag.core.engine import Engine

    root, _ = workspace
    db = root / "knowledge.lbdb"
    with Engine(GragConfig(db_path=db, buffer_pool_size=128 * 1024 * 1024)):
        pass
    saved(root, env={"GRAG_EMBED_PROVIDER": "", "GRAG_AUTO_REFRESH_CODE": "0"})
    report, _, private = collect(db)
    result = diagnostics.verify(report, private, "claude:project:grag", 30)
    assert result["status"] == "verified", result


def test_known_setup_provenance_and_missing_indexed_root_are_visible(workspace):
    from grag.client import GraphClient
    from grag.core.types import DefineSchemaRequest, QueryRequest, UpsertNodesRequest

    root, _ = workspace
    db = root / "knowledge.lbdb"
    entry = saved(root, env={"GRAG_EMBED_PROVIDER": "", "GRAG_AUTO_REFRESH_CODE": "0"})
    asyncio.run(onboarding.verify_entry(entry, root))
    config = GragConfig(db_path=db, buffer_pool_size=128 * 1024 * 1024)
    query = QueryRequest(cypher="MATCH (n:GragSetup) RETURN n.nonce")
    with GraphClient(config) as client:
        before = client.call("cypher_query", query)["rows"]
        client.call("define_schema", DefineSchemaRequest(node_tables=[{"name": "Repo", "properties": [{"name": "path"}], "searchable": False}]))
        client.call("upsert_nodes", UpsertNodesRequest(nodes=[{"label": "Repo", "key": "old", "properties": {"path": str(root / "moved")}}]))
    report, _, private = collect(db)
    result = diagnostics.verify(report, private, "claude:project:grag", 30)
    assert result["status"] == "verified", result
    assert result["setup_provenance"] == onboarding.SETUP_SOURCE
    assert result["indexed_roots"] == [{"path": str(root / "moved"), "exists": False}]
    with GraphClient(config) as client:
        assert client.call("cypher_query", query)["rows"] == before


def test_historical_wal_error_is_not_attributed_to_new_launcher_failure(workspace, monkeypatch):
    root, _ = workspace
    db = root / "knowledge.lbdb"
    db.touch()
    saved(root, args=["-m", "grag.cli", "--db", str(db), "mcp", "--auto-serve"])
    path = admin.log_path(db)
    path.parent.mkdir(parents=True)
    path.write_text("Database replay failed: Corrupted wal file.\n")
    report, _, private = collect(db)

    async def fail(*args, **kwargs):
        with path.open("a") as stream:
            stream.write("Current failure: address already in use\n")
        raise RuntimeError("connection closed")

    monkeypatch.setattr(onboarding, "verify_entry", fail)
    result = diagnostics.verify(report, private, "claude:project:grag", 1)
    assert "address already in use" in result["detail"]
    assert "Corrupted wal" not in result["detail"]
    assert "failed WAL replay" not in result["summary"]
