from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from grag.cli import main
from grag.code_state import index_records, registered_repo_ids, saved_request
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.types import CodeIngestRequest
from grag.ingest.code import ingest_code
from grag.project_files import ProjectConfigError, apply_ops
from grag.project_identity import identity_ops, new_identity, read_identity
from grag.relocation import plan_graph, relocate_checkout


@pytest.fixture
def moved(tmp_path, monkeypatch):
    # Isolate grag's config/backups while allowing the native runtime to LOAD
    # the same cached extensions as the rest of the engine test suite.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("GRAG_DB_PATH", raising=False)
    monkeypatch.delenv("GRAG_DB_DIR", raising=False)
    monkeypatch.setattr("grag.admin.find_server", lambda *_: None)
    monkeypatch.setattr("grag.project._grag_bin", lambda: "/runtime/grag")
    old, new = tmp_path / "before", tmp_path / "after"
    old.mkdir()
    file = old / "code.py"
    file.write_text("def hello():\n    return 42\n")
    config = GragConfig(
        db_path=tmp_path / "memory.lbdb", buffer_pool_size=128 * 1024**2
    )
    identity = new_identity(old, config.db_path, 43211)
    apply_ops(list(identity_ops(old, identity)))
    with Engine(config) as engine:
        ingest_code(
            engine,
            config,
            CodeIngestRequest(paths=[str(file)], calls=False, max_file_kb=17),
        )
        function = engine.execute("MATCH (f:Function) RETURN f.id").rows[0][0]
        engine.execute_write(
            "CREATE NODE TABLE Memory(key INT64 PRIMARY KEY, body STRING, _source STRING)"
        )
        engine.execute_write(
            "CREATE REL TABLE EXPLAINS(FROM Memory TO Function, _source STRING)"
        )
        engine.execute_write(
            "CREATE (m:Memory {key: 7, body: $body, _source: $source})",
            {
                "body": f"Rationale mentions {old}; keep the original wording",
                "source": str(file),
            },
        )
        engine.execute_write(
            "MATCH (m:Memory), (f:Function {id: $id}) CREATE (m)-[:EXPLAINS {_source: $source}]->(f)",
            {"id": function, "source": str(file)},
        )
        # A source whose string prefix matches, but is outside the checkout.
        engine.execute_write(
            "CREATE (m:Memory {key: 8, body: 'Other', _source: $source})",
            {"source": str(old) + "-other/code.py"},
        )
    (old / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other": {"command": "keep-me"},
                    "grag": {
                        "command": str(old / ".venv/bin/grag"),
                        "args": ["--db", str(config.db_path), "mcp", "--port", "43211"],
                        "env": {"KEEP": "yes"},
                    },
                }
            }
        )
    )
    old.rename(new)
    monkeypatch.chdir(new)
    return config, old, new, function


def test_move_preserves_ids_authored_relationships_scope_and_reingest(moved):
    config, old, new, function = moved
    with Engine(config, read_only=True) as engine:
        before_ids = registered_repo_ids(engine)
    relocate_checkout(config, old, new)
    identity = read_identity(new)
    assert identity.root == str(new) and identity.db_path == str(config.db_path)
    with Engine(config) as engine:
        record = index_records(engine)[str(new)]
        assert record["_index_generation"] is None
        request = saved_request(new, record["_index_options"])
        assert request.paths == [str(new / "code.py")]
        assert request.calls is False and request.max_file_kb == 17
        assert registered_repo_ids(engine)[str(new)] == before_ids[str(old)]
        assert engine.execute(
            "MATCH (m:Memory {key: 7})-[:EXPLAINS]->(f:Function) RETURN m.body, m._source, f.id, f._source"
        ).rows == [
            [
                f"Rationale mentions {old}; keep the original wording",
                str(new / "code.py"),
                function,
                str(new / "code.py"),
            ]
        ]
        assert engine.execute("MATCH (m:Memory {key: 8}) RETURN m._source").rows == [
            [str(old) + "-other/code.py"]
        ]
        assert engine.execute("MATCH ()-[r:EXPLAINS]->() RETURN r._source").rows == [
            [str(new / "code.py")]
        ]
        (new / "unrequested.py").write_text("def excluded(): pass\n")
        (new / "code.py").write_text("def hello():\n    return 99\n")
        ingest_code(engine, config, request)
        assert engine.execute(
            "MATCH (:Memory)-[:EXPLAINS]->(f:Function) RETURN f.id"
        ).rows == [[function]]
        assert engine.execute("MATCH (m:Module) RETURN m.path").rows == [["code.py"]]
    # Reopen, re-ingest, and a repeated relocation must all retain the same IDs.
    relocate_checkout(config, old, new)
    with Engine(config, read_only=True) as engine:
        assert engine.execute(
            "MATCH (:Memory)-[:EXPLAINS]->(f:Function) RETURN f.id"
        ).rows == [[function]]
    entry = json.loads((new / ".mcp.json").read_text())["mcpServers"]
    assert entry["other"] == {"command": "keep-me"}
    assert entry["grag"]["command"] == "/runtime/grag"
    assert entry["grag"]["env"] == {"KEEP": "yes"}


def tree_bytes(root: Path):
    return {
        str(p.relative_to(root)): (
            p.read_bytes(),
            p.stat().st_mode,
            p.stat().st_mtime_ns,
        )
        for p in root.rglob("*")
        if p.is_file()
    }


def test_dry_run_is_read_only_for_database_sidecars_modes_and_files(moved):
    config, old, new, _ = moved
    config.db_path.chmod(0o644)
    before = tree_bytes(new.parent)
    relocate_checkout(config, old, new, dry_run=True)
    assert tree_bytes(new.parent) == before
    with Engine(config, read_only=True) as engine:
        assert str(old) in index_records(engine)


def test_cli_resolves_moved_mapping_from_subdirectory(moved, monkeypatch):
    config, old, new, _ = moved
    child = new / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert main(["relocate", str(old), str(new), "--dry-run"]) == 0
    assert main(["relocate", str(old), str(new)]) == 0
    assert read_identity(new).db_path == str(config.db_path)


def test_database_inside_moved_checkout_reuses_existing_file(moved):
    config, old, new, _ = moved
    current_db = new / "local.lbdb"
    config.db_path.rename(current_db)
    identity = read_identity(new, allow_moved=True)
    apply_ops(
        list(identity_ops(new, replace(identity, db_path=str(old / "local.lbdb"))))
    )
    data = json.loads((new / ".mcp.json").read_text())
    data["mcpServers"]["grag"]["args"][1] = str(old / "local.lbdb")
    (new / ".mcp.json").write_text(json.dumps(data))
    assert main(["relocate", str(old), str(new)]) == 0
    assert read_identity(new).db_path == str(current_db)
    assert json.loads((new / ".mcp.json").read_text())["mcpServers"]["grag"]["args"][
        1
    ] == str(current_db)
    assert not (old / "local.lbdb").exists()


def test_graph_updates_roll_back_and_files_remain_untouched_on_failure(
    moved, monkeypatch
):
    config, old, new, _ = moved
    before = tree_bytes(new)
    real = Engine.execute_write
    writes = 0

    def fail(self, query, params=None):
        nonlocal writes
        if query.startswith("MATCH") and " SET " in query:
            writes += 1
            if writes == 2:
                raise RuntimeError("injected relocation failure")
        return real(self, query, params)

    monkeypatch.setattr(Engine, "execute_write", fail)
    with pytest.raises(RuntimeError, match="injected"):
        relocate_checkout(config, old, new)
    assert tree_bytes(new) == before
    with Engine(config, read_only=True) as engine:
        assert str(old) in index_records(engine)
        assert engine.execute("MATCH (f:Function) RETURN f._source").rows == [
            [str(old / "code.py")]
        ]


def test_client_publication_failure_is_retryable_after_graph_commit(moved, monkeypatch):
    import grag.relocation as relocation

    config, old, new, _ = moved
    real = relocation.apply_ops

    def fail(_):
        raise ProjectConfigError("injected file failure")

    monkeypatch.setattr(relocation, "apply_ops", fail)
    with pytest.raises(ProjectConfigError, match="Graph paths committed"):
        relocate_checkout(config, old, new)
    with Engine(config, read_only=True) as engine:
        assert str(new) in index_records(engine)
    monkeypatch.setattr(relocation, "apply_ops", real)
    relocate_checkout(config, old, new)
    assert read_identity(new).root == str(new)


def test_invalid_client_blocks_graph_mutation(moved):
    config, old, new, _ = moved
    (new / ".mcp.json").write_text("{bad")
    before = config.db_path.read_bytes()
    with pytest.raises(ProjectConfigError):
        relocate_checkout(config, old, new)
    assert config.db_path.read_bytes() == before


@pytest.mark.parametrize("case", ["copy", "missing", "overlap", "same", "server"])
def test_unsafe_destinations_and_live_server_are_rejected(moved, monkeypatch, case):
    config, old, new, _ = moved
    if case == "copy":
        old.mkdir()
    elif case == "missing":
        new = new.parent / "missing"
    elif case == "overlap":
        new = old / "child"
    elif case == "same":
        new = old
    else:
        monkeypatch.setattr("grag.admin.find_server", lambda *_: {"port": 1})
    with pytest.raises(ProjectConfigError):
        relocate_checkout(config, old, new)


def test_missing_database_is_not_recreated(moved):
    config, old, new, _ = moved
    missing = config.db_path.parent / "missing.lbdb"
    config.db_path = missing
    with pytest.raises(ProjectConfigError, match="does not exist"):
        relocate_checkout(config, old, new)
    assert not missing.exists()


def test_index_destination_collision_is_rejected(moved):
    config, old, new, _ = moved
    with Engine(config) as engine:
        ingest_code(engine, config, CodeIngestRequest(paths=[str(new)]))
        with pytest.raises(GragError, match="already has an index"):
            plan_graph(engine, old, new)


def test_corrupt_saved_policy_is_not_replaced_with_defaults(moved):
    config, old, new, _ = moved
    with Engine(config) as engine:
        engine.execute_write("MATCH (r:Repo) SET r._index_options = 'invalid'")
    with pytest.raises(GragError, match="Invalid saved indexing options"):
        relocate_checkout(config, old, new)
    with Engine(config, read_only=True) as engine:
        assert str(old) in index_records(engine)


def test_legacy_index_without_policy_keeps_identity_and_remains_unknown(moved):
    config, old, new, _ = moved
    with Engine(config) as engine:
        engine.execute_write(
            "MATCH (r:Repo) SET r.ingested_at = NULL, r._index_options = NULL, r._index_generation = NULL"
        )
        original_id = registered_repo_ids(engine)[str(old)]
    relocate_checkout(config, old, new)
    with Engine(config) as engine:
        assert index_records(engine)[str(new)]["_index_options"] is None
        ingest_code(engine, config, CodeIngestRequest(paths=[str(new)]))
        assert registered_repo_ids(engine)[str(new)] == original_id


def test_read_only_engine_does_not_create_database(tmp_path):
    db = tmp_path / "nested/missing.lbdb"
    with pytest.raises(GragError, match="existing database"):
        Engine(GragConfig(db_path=db), read_only=True)
    assert not db.parent.exists()


def test_freshness_verifies_relocated_scope_and_preserves_links(moved):
    from grag.core.types import ReadPolicy
    from grag.service import GragService

    config, old, new, function = moved
    relocate_checkout(config, old, new)
    (new / "unrequested.py").write_text("def excluded(): pass\n")
    svc = GragService(config)
    try:
        svc.enable_auto_refresh()
        report = svc.read_freshness(
            ReadPolicy(freshness="require", freshness_timeout_ms=5000)
        )
        assert report.status == "fresh"
        assert svc.engine.execute(
            "MATCH (:Memory)-[:EXPLAINS]->(f:Function) RETURN f.id"
        ).rows == [[function]]
        assert svc.engine.execute("MATCH (m:Module) RETURN m.path").rows == [
            ["code.py"]
        ]
    finally:
        svc.close()


def test_read_only_preview_of_graph_with_fts_index_changes_no_files(moved):
    from grag.retrieval.search import _ensure_fts_index

    config, old, new, _ = moved
    with Engine(config) as engine:
        engine.load_extension("FTS")
        _ensure_fts_index(engine, "Memory", "grag_fts__Memory", ["body"])
    before = tree_bytes(new.parent)
    relocate_checkout(config, old, new, dry_run=True)
    assert tree_bytes(new.parent) == before
    relocate_checkout(config, old, new)
    with Engine(config, read_only=True) as engine:
        rows = engine.execute(
            "CALL QUERY_FTS_INDEX('Memory', 'grag_fts__Memory', 'Rationale') RETURN node.key"
        ).rows
        assert rows == [[7]]


def test_relocate_can_resolve_destination_from_outside_checkout(moved, monkeypatch):
    _, old, new, _ = moved
    monkeypatch.chdir(new.parent)
    assert main(["relocate", str(old), str(new)]) == 0
    assert read_identity(new).root == str(new)


def test_legacy_basename_index_is_not_rekeyed(tmp_path):
    from grag.core.mutate import define_schema
    from grag.core.types import DefineSchemaRequest
    from grag.ingest.code import _CODE_NODE_TABLES, _CODE_REL_TABLES

    root = tmp_path / "pkg"
    root.mkdir()
    source = root / "code.py"
    source.write_text("def hello(): pass\n")
    config = GragConfig(db_path=tmp_path / "old.lbdb", buffer_pool_size=128 * 1024**2)
    with Engine(config) as engine:
        define_schema(
            engine,
            config,
            DefineSchemaRequest(
                node_tables=list(_CODE_NODE_TABLES), rel_tables=list(_CODE_REL_TABLES)
            ),
        )
        engine.execute_write(
            "CREATE (:Repo {id: 'pkg', name: 'pkg', path: $root})", {"root": str(root)}
        )
        engine.execute_write(
            "CREATE (:Module {id: 'pkg:code.py', name: 'code', path: 'code.py', _source: $source})",
            {"source": str(source)},
        )
        engine.execute_write(
            "MATCH (r:Repo), (m:Module) CREATE (r)-[:CONTAINS_REPO_MODULE]->(m)"
        )
        ingest_code(engine, config, CodeIngestRequest(paths=[str(root)]))
        assert registered_repo_ids(engine) == {str(root): "pkg"}
        assert engine.execute("MATCH (m:Module) RETURN m.id").rows == [["pkg:code.py"]]


def test_missing_moved_index_subdirectory_blocks_whole_plan(moved):
    config, old, new, _ = moved
    with Engine(config) as engine:
        # A registered second scope was not included in the folder move.
        engine.execute_write(
            "CREATE (:Repo {id: 'missing', path: $path, ingested_at: 'before'})",
            {"path": str(old / "missing")},
        )
    with pytest.raises(GragError, match="directory is missing"):
        relocate_checkout(config, old, new)
    with Engine(config, read_only=True) as engine:
        assert str(old) in index_records(engine)


def test_mapping_can_move_before_first_database_use(moved, capsys):
    config, old, new, _ = moved
    config.db_path.unlink()
    relocate_checkout(config, old, new, dry_run=True)
    assert read_identity(new, allow_moved=True).root == str(old)
    relocate_checkout(config, old, new)
    assert read_identity(new).root == str(new)
    assert not config.db_path.exists()
    assert "No database was created" in capsys.readouterr().out
