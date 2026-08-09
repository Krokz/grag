"""Code ingestion tests: Python AST parsing into Repo/Module/Class/Function
nodes, stable <repo>:<path> / #qualname ids, CONTAINS/IMPORTS/INHERITS/CALLS
edges, idempotent re-ingest, calls=False, and walk filters (skip dirs,
oversized files, unsupported extensions)."""

from __future__ import annotations

from pathlib import Path

from grag.core.types import CodeIngestRequest
from grag.ingest.code import ingest_code

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
    rows = engine.execute(
        f"MATCH (a)-[r:{rel}]->(b) RETURN a.id, b.id"
    ).rows
    return {(r[0], r[1]) for r in rows}


# --- graph shape -----------------------------------------------------------------


def test_ingest_code_builds_expected_graph(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 2, 4)
    # 2 repo->module + 2 module->class + 2 module->function + 2 class->method
    # + 1 IMPORTS + 1 INHERITS + 3 CALLS (incl. cross-module run->core.helper)
    assert resp.edges == 13

    # node ids follow <repo>:<relative/path> and <module_id>#<qualname>
    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.name, m.language ORDER BY m.id").rows
    assert rows == [
        ["pkg:core.py", "core", "python"],
        ["pkg:main.py", "main", "python"],
    ]
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == ["pkg:core.py#Base", "pkg:core.py#Greeter"]
    rows = engine.execute("MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id").rows
    assert [r[0] for r in rows] == [
        "pkg:core.py#Greeter.greet",
        "pkg:core.py#Greeter.greet_loudly",
        "pkg:core.py#helper",
        "pkg:main.py#run",
    ]
    assert [r[1] for r in rows] == [True, True, False, False]

    # structure-only nodes: signature/docstring/lines, no source bodies
    row = engine.execute(
        "MATCH (f:Function) WHERE f.id = 'pkg:core.py#Greeter.greet' "
        "RETURN f.signature, f.docstring, f.line_start, f.line_end, f.path"
    ).rows[0]
    assert row[0] == "def greet(self, name: str) -> str:"
    assert row[1] == "Greet someone."
    assert row[2] == 11 and row[3] == 13
    assert row[4] == "core.py"

    # edges
    assert _edge_pairs(engine, "CONTAINS_REPO_MODULE") == {
        ("pkg", "pkg:core.py"),
        ("pkg", "pkg:main.py"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_CLASS") == {
        ("pkg:core.py", "pkg:core.py#Base"),
        ("pkg:core.py", "pkg:core.py#Greeter"),
    }
    assert _edge_pairs(engine, "CONTAINS_CLASS_FUNCTION") == {
        ("pkg:core.py#Greeter", "pkg:core.py#Greeter.greet"),
        ("pkg:core.py#Greeter", "pkg:core.py#Greeter.greet_loudly"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_FUNCTION") == {
        ("pkg:core.py", "pkg:core.py#helper"),
        ("pkg:main.py", "pkg:main.py#run"),
    }
    assert _edge_pairs(engine, "IMPORTS") == {("pkg:main.py", "pkg:core.py")}
    assert _edge_pairs(engine, "INHERITS") == {
        ("pkg:core.py#Greeter", "pkg:core.py#Base")
    }
    assert _edge_pairs(engine, "CALLS") == {
        ("pkg:core.py#Greeter.greet", "pkg:core.py#helper"),  # bare name call (same module)
        ("pkg:core.py#Greeter.greet_loudly", "pkg:core.py#Greeter.greet"),  # self.<m>
        ("pkg:main.py#run", "pkg:core.py#helper"),  # bare call to from-imported function
    }


def test_ingest_code_is_idempotent(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    req = CodeIngestRequest(paths=[str(pkg)])
    first = ingest_code(engine, engine.config, req)
    second = ingest_code(engine, engine.config, req)

    assert second.model_dump() == first.model_dump()
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


def test_ingest_code_calls_false_skips_call_edges(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], calls=False)
    )

    assert resp.edges == 10  # everything but the 2 CALLS
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 0
    assert _count(engine, "MATCH ()-[r:IMPORTS]->() RETURN count(*)") == 1
    assert _count(engine, "MATCH (f:Function) RETURN count(*)") == 4


def test_ingest_code_multiple_repos_and_cross_repo_inherits(engine, tmp_path):
    _write_pkg(tmp_path, "pkg")
    other = tmp_path / "other"
    other.mkdir()
    (other / "sub.py").write_text(
        "from pkg.core import Greeter\n\n\nclass Loud(Greeter):\n    pass\n",
        encoding="utf-8",
    )
    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(tmp_path / "pkg"), str(other)])
    )

    assert resp.repos == 2 and resp.modules == 3
    # `from pkg.core import Greeter` resolves by dotted suffix across repos
    assert ("other:sub.py", "pkg:core.py") in _edge_pairs(engine, "IMPORTS")
    # base `Greeter` is globally unique across the scanned set
    assert ("other:sub.py#Loud", "pkg:core.py#Greeter") in _edge_pairs(engine, "INHERITS")


# --- walk filters and warnings ------------------------------------------------------


def test_walk_skips_and_warns(engine, tmp_path):
    pkg = _write_pkg(tmp_path)
    (pkg / ".git").mkdir()
    (pkg / ".git" / "ignored.py").write_text("def nope():\n    pass\n", encoding="utf-8")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "cached.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "big.py").write_text("x = 1\n" * 500, encoding="utf-8")  # ~3KB
    (pkg / "tool.go").write_text("package main\n", encoding="utf-8")
    (pkg / "notes.md").write_text("# not code\n", encoding="utf-8")

    resp = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(pkg)], max_file_kb=1)
    )

    assert resp.modules == 2  # only core.py and main.py
    assert any("big.py" in w and "max_file_kb=1" in w for w in resp.warnings)
    assert any("'.go'" in w and "no parser registered" in w for w in resp.warnings)
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
    assert rows == [["pkg:main.py"]]  # repo is the file's parent dir


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
    assert "pkg:real.py" in ids
    assert not any("index-C2RYf8Fw" in i or "app.min" in i for i in ids)


# --- CALLS: imported + lazy/conditional calls ----------------------------------------


def test_calls_resolve_from_imported_functions(engine, tmp_path):
    """A bare `helper(...)` call where `helper` comes from `from core import helper`
    resolves across modules (the vector_candidates dogfooding case)."""
    pkg = _write_pkg(tmp_path)
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    assert ("pkg:main.py#run", "pkg:core.py#helper") in _edge_pairs(engine, "CALLS")


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
    assert ("pkg:search.py#search_knowledge", "pkg:engine.py#vector_candidates") in _edge_pairs(
        engine, "CALLS"
    )
