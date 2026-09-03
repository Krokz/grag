"""Code ingestion tests: Python AST parsing into Repo/Module/Class/Function
nodes, stable <repo-id>:<path> / #qualname ids, CONTAINS/IMPORTS/INHERITS/CALLS
edges, idempotent re-ingest, calls=False, and walk filters (skip dirs,
oversized files, unsupported extensions)."""

from __future__ import annotations

from pathlib import Path

from grag.core.types import CodeIngestRequest
from grag.ingest.code import _repo_id, ingest_code

CORE_PY = '''"""Core module."""


class Base:
    """Shared base."""


class Greeter(Base):
    """Says hi."""

    def greet(self, name: str) -> str:
        """Greet someone."""
        return f"hi {name} {helper(1)}"

    def greet_loudly(self, name: str) -> str:
        return self.greet(name).upper()


def helper(x: int) -> int:
    """Double it."""
    return x * 2
'''

MAIN_PY = '''"""Main module."""

from core import helper


def run() -> int:
    return helper(21)
'''


def _write_pkg(root: Path, name: str = "pkg") -> Path:
    """Two modules: one defines a class with methods plus a function, the
    other imports the first and calls its function."""
    pkg = root / name
    pkg.mkdir()
    (pkg / "core.py").write_text(CORE_PY, encoding="utf-8")
    (pkg / "main.py").write_text(MAIN_PY, encoding="utf-8")
    return pkg


def _count(engine, cypher: str) -> int:
    return int(engine.execute(cypher).rows[0][0])


def _edge_pairs(engine, rel: str) -> set[tuple[str, str]]:
    rows = engine.execute(f"MATCH (a)-[r:{rel}]->(b) RETURN a.id, b.id").rows
    return {(r[0], r[1]) for r in rows}


def _mid(root: Path, relative: str) -> str:
    return f"{_repo_id(root)}:{relative}"


# --- graph shape -----------------------------------------------------------------


def test_ingest_code_builds_expected_graph(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    repo = _repo_id(pkg)
    core = _mid(pkg, "core.py")
    main = _mid(pkg, "main.py")
    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 2, 4)
    # 2 repo->module + 2 module->class + 2 module->function + 2 class->method
    # + 1 IMPORTS + 1 INHERITS + 3 CALLS (incl. cross-module run->core.helper)
    assert resp.edges == 13

    # node ids follow <repo-id>:<relative/path> and <module_id>#<qualname>
    rows = engine.execute(
        "MATCH (m:Module) RETURN m.id, m.name, m.language ORDER BY m.id"
    ).rows
    assert rows == [
        [core, "core", "python"],
        [main, "main", "python"],
    ]
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == [f"{core}#Base", f"{core}#Greeter"]
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id"
    ).rows
    assert [r[0] for r in rows] == [
        f"{core}#Greeter.greet",
        f"{core}#Greeter.greet_loudly",
        f"{core}#helper",
        f"{main}#run",
    ]
    assert [r[1] for r in rows] == [True, True, False, False]

    # structure-only nodes: signature/docstring/lines, no source bodies
    row = engine.execute(
        "MATCH (f:Function) WHERE f.id = $id "
        "RETURN f.signature, f.docstring, f.line_start, f.line_end, f.path",
        {"id": f"{core}#Greeter.greet"},
    ).rows[0]
    assert row[0] == "def greet(self, name: str) -> str:"
    assert row[1] == "Greet someone."
    assert row[2] == 11 and row[3] == 13
    assert row[4] == "core.py"

    # edges
    assert _edge_pairs(engine, "CONTAINS_REPO_MODULE") == {
        (repo, core),
        (repo, main),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_CLASS") == {
        (core, f"{core}#Base"),
        (core, f"{core}#Greeter"),
    }
    assert _edge_pairs(engine, "CONTAINS_CLASS_FUNCTION") == {
        (f"{core}#Greeter", f"{core}#Greeter.greet"),
        (f"{core}#Greeter", f"{core}#Greeter.greet_loudly"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_FUNCTION") == {
        (core, f"{core}#helper"),
        (main, f"{main}#run"),
    }
    assert _edge_pairs(engine, "IMPORTS") == {(main, core)}
    assert _edge_pairs(engine, "INHERITS") == {(f"{core}#Greeter", f"{core}#Base")}
    assert _edge_pairs(engine, "CALLS") == {
        (
            f"{core}#Greeter.greet",
            f"{core}#helper",
        ),  # bare name call (same module)
        (f"{core}#Greeter.greet_loudly", f"{core}#Greeter.greet"),
        (
            f"{main}#run",
            f"{core}#helper",
        ),  # bare call to from-imported function
    }


def test_ingest_code_is_idempotent(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    first = ingest_code(engine, engine.config, req)
    second = ingest_code(engine, engine.config, req)

    # Same graph both times; the second run recognises every file as unchanged.
    assert second.model_dump(exclude={"files_unchanged"}) == first.model_dump(
        exclude={"files_unchanged"}
    )
    assert first.files_unchanged == 0
    assert second.files_unchanged == second.files_parsed == 2
    for label, n in (("Repo", 1), ("Module", 2), ("Class", 2), ("Function", 4)):
        assert _count(engine, f"MATCH (n:{label}) RETURN count(*)") == n
    for rel, n in (
        ("CONTAINS_REPO_MODULE", 2),
        ("CONTAINS_MODULE_CLASS", 2),
        ("CONTAINS_MODULE_FUNCTION", 2),
        ("CONTAINS_CLASS_FUNCTION", 2),
        ("IMPORTS", 1),
        ("INHERITS", 1),
        ("CALLS", 3),
    ):
        assert _count(engine, f"MATCH ()-[r:{rel}]->() RETURN count(*)") == n


def test_ingest_code_same_basename_repos_do_not_collide(engine, tmp_path):
    left_parent = tmp_path / "left"
    right_parent = tmp_path / "right"
    left_parent.mkdir()
    right_parent.mkdir()
    left = _write_pkg(left_parent)
    right = _write_pkg(right_parent)

    resp = ingest_code(
        engine,
        engine.config,
        CodeIngestRequest(paths=[str(left), str(right)]),
    )

    assert resp.repos == 2 and resp.modules == 4
    repo_ids = {row[0] for row in engine.execute("MATCH (r:Repo) RETURN r.id").rows}
    assert repo_ids == {_repo_id(left), _repo_id(right)}
    assert _repo_id(left) != _repo_id(right)


def test_reingest_prunes_removed_symbols_and_generated_edges(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    removed_id = f"{_mid(pkg, 'core.py')}#Greeter.greet_loudly"
    method = (
        "\n    def greet_loudly(self, name: str) -> str:\n"
        "        return self.greet(name).upper()\n"
    )
    (pkg / "core.py").write_text(CORE_PY.replace(method, ""), encoding="utf-8")

    resp = ingest_code(engine, engine.config, req)

    assert resp.nodes_pruned == 1
    assert resp.edges_pruned == 2
    assert engine.execute(
        "MATCH (f:Function) WHERE f.id = $id RETURN count(f)", {"id": removed_id}
    ).rows == [[0]]
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 2


def test_reingest_directory_prunes_deleted_source_file(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    (pkg / "main.py").unlink()

    resp = ingest_code(engine, engine.config, req)

    assert resp.nodes_pruned == 2  # deleted Module + its Function
    assert resp.edges_pruned == 4
    assert _count(engine, "MATCH (m:Module) RETURN count(*)") == 1
    assert _count(engine, "MATCH (f:Function) RETURN count(*)") == 3


def test_reingest_migrates_legacy_basename_only_ids(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    ingest_code(engine, engine.config, req)
    engine.execute_write(
        "CREATE (r:Repo {id: $id, name: $name, path: $path})",
        {"id": "pkg", "name": "pkg", "path": str(pkg.resolve())},
    )
    engine.execute_write(
        "CREATE (m:Module {id: $id, path: $path, language: $language, "
        "name: $name, _source: $source})",
        {
            "id": "pkg:legacy.py",
            "path": "legacy.py",
            "language": "python",
            "name": "legacy",
            "source": str(pkg / "legacy.py"),
        },
    )

    resp = ingest_code(engine, engine.config, req)

    assert resp.nodes_pruned == 2
    ids = {row[0] for row in engine.execute("MATCH (m:Module) RETURN m.id").rows}
    assert "pkg:legacy.py" not in ids


def test_ingest_code_calls_false_skips_call_edges(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], calls=False)
    )

    assert resp.edges == 10  # everything but the 2 CALLS
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 0
    assert _count(engine, "MATCH ()-[r:IMPORTS]->() RETURN count(*)") == 1
    assert _count(engine, "MATCH (f:Function) RETURN count(*)") == 4


def test_reingest_calls_false_prunes_previous_call_edges(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], calls=False)
    )

    assert resp.edges_pruned == 3
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 0


def test_ingest_code_multiple_repos_and_cross_repo_inherits(engine, tmp_path):
    pkg = _write_pkg(tmp_path, "pkg")
    other = tmp_path / "other"
    other.mkdir()
    (other / "sub.py").write_text(
        "from pkg.core import Greeter\n\n\nclass Loud(Greeter):\n    pass\n",
        encoding="utf-8",
    )
    resp = ingest_code(
        engine,
        engine.config,
        CodeIngestRequest(paths=[str(tmp_path / "pkg"), str(other)]),
    )

    assert resp.repos == 2 and resp.modules == 3
    # `from pkg.core import Greeter` resolves by dotted suffix across repos
    assert (_mid(other, "sub.py"), _mid(pkg, "core.py")) in _edge_pairs(
        engine, "IMPORTS"
    )
    # base `Greeter` is globally unique across the scanned set
    assert (
        f"{_mid(other, 'sub.py')}#Loud",
        f"{_mid(pkg, 'core.py')}#Greeter",
    ) in _edge_pairs(engine, "INHERITS")


# --- walk filters and warnings ------------------------------------------------------


def test_walk_skips_and_warns(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    (pkg / ".git").mkdir()
    (pkg / ".git" / "ignored.py").write_text(
        "def nope():\n    pass\n", encoding="utf-8"
    )
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "cached.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "big.py").write_text("x = 1\n" * 500, encoding="utf-8")  # ~3KB
    (pkg / "tool.pl").write_text("sub main { }\n", encoding="utf-8")
    (pkg / "notes.md").write_text("# not code\n", encoding="utf-8")

    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], max_file_kb=1)
    )

    assert resp.modules == 2  # only core.py and main.py
    assert any("big.py" in w and "max_file_kb=1" in w for w in resp.warnings)
    assert any("'.pl'" in w and "no parser registered" in w for w in resp.warnings)
    assert not any("notes.md" in w for w in resp.warnings)  # non-code: silent
    assert not any("ignored.py" in w or "cached.py" in w for w in resp.warnings)


def test_ingest_code_missing_path_and_syntax_error_warn(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    (pkg / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    resp = ingest_code(
        engine,
        engine.config,
        CodeIngestRequest(paths=[str(pkg), str(tmp_path / "ghost")]),
    )

    assert resp.modules == 2  # broken.py skipped, the rest ingested
    assert any("ghost" in w and "no such file or directory" in w for w in resp.warnings)
    assert any("broken.py" in w and "could not parse" in w for w in resp.warnings)


def test_ingest_code_single_file_path(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg / "main.py")])
    )

    assert (resp.repos, resp.modules, resp.functions) == (1, 1, 1)
    rows = engine.execute("MATCH (m:Module) RETURN m.id").rows
    assert rows == [[_mid(pkg, "main.py")]]


def test_walk_skips_built_bundle_and_minified(engine, tmp_path):
    """Built artifacts (minified + content-hashed bundles like Vite's
    `index-C2RYf8Fw.js`) are build output, not source — never indexed."""
    pkg = _write_pkg(tmp_path)
    assets = pkg / "static" / "assets"
    assets.mkdir(parents=True)
    (assets / "index-C2RYf8Fw.js").write_text("function Cd(){}", encoding="utf-8")
    (assets / "app.min.js").write_text("function x(){}", encoding="utf-8")
    (pkg / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    assert resp.modules == 3  # core.py, main.py, real.py — not the bundles
    rows = engine.execute("MATCH (m:Module) RETURN m.id").rows
    ids = [r[0] for r in rows]
    assert _mid(pkg, "real.py") in ids
    assert not any("index-C2RYf8Fw" in i or "app.min" in i for i in ids)


# --- CALLS: imported + lazy/conditional calls ----------------------------------------


def test_calls_resolve_from_imported_functions(engine, tmp_path):
    """A bare `helper(...)` call where `helper` comes from `from core import helper`
    resolves across modules (the vector_candidates dogfooding case)."""
    pkg = _write_pkg(tmp_path)
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    assert (
        f"{_mid(pkg, 'main.py')}#run",
        f"{_mid(pkg, 'core.py')}#helper",
    ) in _edge_pairs(engine, "CALLS")


def test_calls_resolve_function_local_lazy_import_inside_try(engine, tmp_path):
    """A guarded lazy import (inside the function body, under try/except) still
    yields a CALLS edge — the walker descends into try/conditional bodies and the
    from-import resolution is not fooled by the guard."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "engine.py").write_text(
        "def vector_candidates():\n    return []\n", encoding="utf-8"
    )
    (pkg / "search.py").write_text(
        '""" searcher """\n\n\n'
        "def search_knowledge():\n"
        "    try:\n"
        "        from engine import vector_candidates\n"
        "        return vector_candidates()\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8",
    )
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    assert (
        f"{_mid(pkg, 'search.py')}#search_knowledge",
        f"{_mid(pkg, 'engine.py')}#vector_candidates",
    ) in _edge_pairs(engine, "CALLS")


# --- staleness metadata (git_commit / git_branch / ingested_at) -------------------


def test_repo_carries_ingested_at_outside_git(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    rows = engine.execute(
        "MATCH (r:Repo) RETURN r.ingested_at, r.git_commit"
    ).rows
    assert len(rows) == 1
    ingested_at, git_commit = rows[0]
    assert ingested_at  # ISO timestamp string
    assert git_commit is None  # tmp_path is not a git checkout


def test_repo_carries_git_commit_inside_git(engine, tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        import pytest

        pytest.skip("git not installed")
    import os

    pkg = _write_pkg(tmp_path)
    # Extend the real environment: git needs more than PATH on Windows
    # (SYSTEMROOT etc.); HOME/config overrides isolate from user gitconfig.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig"),
    }
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(argv, cwd=pkg, env=env, check=True)  # noqa: S603 — fixed git argv

    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    rows = engine.execute(
        "MATCH (r:Repo) RETURN r.git_commit, r.git_branch"
    ).rows
    commit, branch = rows[0]
    assert commit and len(commit) == 40
    assert branch == "main"


def test_reingest_adds_staleness_columns_to_legacy_repo_table(engine, tmp_path):
    """Databases whose Repo table predates the staleness columns get ALTERed."""
    engine.execute_write(
        "CREATE NODE TABLE Repo(id STRING PRIMARY KEY, name STRING, path STRING, "
        "_source STRING, _created_at TIMESTAMP)"
    )
    pkg = _write_pkg(tmp_path)
    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    assert resp.repos == 1
    rows = engine.execute("MATCH (r:Repo) RETURN r.ingested_at").rows
    assert rows and rows[0][0]
