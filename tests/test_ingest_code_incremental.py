"""Incremental code ingest: unchanged files skip the write lock entirely."""

from __future__ import annotations

from pathlib import Path

import pytest

from grag.core.types import CodeIngestRequest
from grag.ingest import code as code_module
from grag.ingest.code import _INGEST_HASH_PROP, _repo_id, ingest_code

CORE_PY = '''"""Core module."""


def helper(x: int) -> int:
    return x * 2
'''

MAIN_PY = '''"""Main module."""

from core import helper, later


def run() -> int:
    return helper(21) + later()
'''


def _write_pkg(root: Path) -> Path:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text(CORE_PY, encoding="utf-8")
    (pkg / "main.py").write_text(MAIN_PY, encoding="utf-8")
    return pkg


def _count(engine, cypher: str) -> int:
    return int(engine.execute(cypher).rows[0][0])


def _call_pairs(engine) -> set[tuple[str, str]]:
    rows = engine.execute("MATCH (a)-[:CALLS]->(b) RETURN a.name, b.name").rows
    return {(r[0], r[1]) for r in rows}


@pytest.fixture()
def write_counter(monkeypatch):
    """Count node/edge upsert calls reaching the mutation layer."""
    calls = {"nodes": 0, "edges": 0}
    real_nodes, real_edges = code_module.upsert_nodes, code_module.upsert_edges

    def nodes(engine, config, req):
        calls["nodes"] += len(req.nodes)
        return real_nodes(engine, config, req)

    def edges(engine, config, req):
        calls["edges"] += len(req.edges)
        return real_edges(engine, config, req)

    monkeypatch.setattr(code_module, "upsert_nodes", nodes)
    monkeypatch.setattr(code_module, "upsert_edges", edges)
    return calls


def test_unchanged_reingest_writes_only_the_repo_node(engine, tmp_path, write_counter):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    first = ingest_code(engine, engine.config, req)
    assert first.files_parsed == 2 and first.files_unchanged == 0
    assert write_counter["nodes"] > 1 and write_counter["edges"] > 0

    write_counter["nodes"] = write_counter["edges"] = 0
    second = ingest_code(engine, engine.config, req)

    assert second.files_parsed == 2 and second.files_unchanged == 2
    # Only the Repo node (ingested_at / git state) is rewritten; no edges.
    assert write_counter == {"nodes": 1, "edges": 0}
    assert second.edges == first.edges  # resolved set, unchanged
    assert second.nodes_pruned == 0 and second.edges_pruned == 0
    # The fingerprint lives on a reserved prop: outside FTS and embedding text.
    rows = engine.execute(
        f"MATCH (m:Module) RETURN m.{_INGEST_HASH_PROP} IS NOT NULL"
    ).rows
    assert rows and all(r[0] for r in rows)


def test_changed_file_rewrites_itself_and_relinks_unchanged_callers(
    engine, tmp_path, write_counter
):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    # main.run -> later() could not resolve yet (later does not exist).
    assert _call_pairs(engine) == {("run", "helper")}

    # Add `later` to core.py: main.py is UNCHANGED, but its dangling call now
    # resolves — the edge must appear without rewriting main.py's nodes.
    (pkg / "core.py").write_text(
        CORE_PY + "\n\ndef later() -> int:\n    return 1\n", encoding="utf-8"
    )
    write_counter["nodes"] = write_counter["edges"] = 0
    resp = ingest_code(engine, engine.config, req)

    assert resp.files_unchanged == 1  # main.py skipped
    assert _call_pairs(engine) == {("run", "helper"), ("run", "later")}
    main_id = f"{_repo_id(pkg)}:main.py"
    # main.py's Module node was not rewritten (only core.py's + Repo + new fn).
    assert write_counter["nodes"] == 1 + 1 + 2  # Repo + core Module + 2 Functions
    assert _count(engine, "MATCH (f:Function) RETURN count(*)") == 3
    assert engine.execute(
        "MATCH (m:Module {id: $id}) RETURN count(m)", {"id": main_id}
    ).rows == [[1]]


def test_changed_file_prunes_its_removed_symbols_only(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)

    (pkg / "core.py").write_text('"""Empty now."""\n', encoding="utf-8")
    resp = ingest_code(engine, engine.config, req)

    assert resp.nodes_pruned == 1  # helper
    assert _count(engine, "MATCH (f:Function) RETURN count(*)") == 1  # run
    assert _call_pairs(engine) == set()  # DETACH DELETE dropped run->helper


def test_parse_options_are_part_of_the_fingerprint(engine, tmp_path, write_counter):
    pkg = _write_pkg(tmp_path)
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 1

    # calls=False must re-process every file (and prune the CALLS edges) even
    # though no source byte changed.
    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], calls=False)
    )
    assert resp.files_unchanged == 0
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 0


def test_incremental_false_forces_full_rewrite(engine, tmp_path, write_counter):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    write_counter["nodes"] = write_counter["edges"] = 0

    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], incremental=False)
    )
    assert resp.files_unchanged == 0
    assert write_counter["nodes"] > 1 and write_counter["edges"] > 0


def test_legacy_modules_without_fingerprint_are_rewritten_once(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    # Simulate a database ingested before fingerprints existed.
    engine.execute_write(f"MATCH (m:Module) SET m.{_INGEST_HASH_PROP} = NULL")

    resp = ingest_code(engine, engine.config, req)
    assert resp.files_unchanged == 0
    again = ingest_code(engine, engine.config, req)
    assert again.files_unchanged == 2
