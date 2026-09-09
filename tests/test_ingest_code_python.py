"""M37: actual scope bindings, negative edges, coverage and dependency refresh."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grag.core.mutate import upsert_edges
from grag.core.types import CodeIngestRequest, UpsertEdge, UpsertEdgesRequest
from grag.ingest.code import _parse_python, _repo_id, ingest_code
from grag.ingest.code_python import resolve


def resolve_sources(sources):
    modules = [
        _parse_python(
            Path("/fixture/pkg") / path,
            source,
            repo="pkg-id",
            rel_path=path,
            calls=True,
        )
        for path, source in sources.items()
    ]
    edges = resolve(modules, calls=True)
    calls = {
        (str(e.from_key).split("#")[1], str(e.to_key).split("#")[1])
        for e in edges
        if e.type == "CALLS"
    }
    return modules, edges, calls


@pytest.mark.parametrize(
    "definition",
    [
        "def run(helper):\n return helper()",
        "def run(*, helper):\n return helper()",
        "def run(**helper):\n return helper()",
        "def run():\n helper()\n helper = lambda: 2",
        "def run():\n helper: object\n return helper()",
        "def run():\n for helper in []:\n  helper()",
        "def run():\n with manager() as helper:\n  helper()",
        "def run():\n try:\n  pass\n except Exception as helper:\n  helper()",
        'def run(value):\n match value:\n  case {"x": helper}:\n   helper()',
        "def run():\n from missing import helper\n return helper()",
        "def run():\n del helper\n return helper()",
        "def run():\n helper := None",  # replaced below with a legal walrus statement
    ],
)
def test_unknown_local_bindings_block_same_named_function(definition):
    definition = definition.replace("helper := None", "(helper := None)\n helper()")
    _, _, calls = resolve_sources(
        {"core.py": "def helper(): return 1\n" + definition + "\n"}
    )
    assert ("run", "helper") not in calls


def test_import_aliases_reexports_and_function_local_scope():
    modules, edges, calls = resolve_sources(
        {
            "core.py": "def helper(): return 1\n",
            "__init__.py": "from .core import helper as exported\n",
            "use.py": """from .core import helper as renamed
import pkg.core as library
from pkg import core as another
from pkg import exported
def run():
 return renamed() + library.helper() + another.helper() + exported()
def lazy():
 try:
  from .core import helper as local
  return local()
 except ImportError:
  return 0
def leaked():
 return local()
""",
        }
    )
    assert calls == {("run", "helper"), ("lazy", "helper")}
    run = next(
        e for e in edges if e.type == "CALLS" and str(e.from_key).endswith("#run")
    )
    assert run.properties["resolution"] == "python_static_binding"
    assert (
        json.loads(run.properties["sites"])["total"] == 1
    )  # four calls on the same line
    coverage = json.loads(modules[-1].module.properties["code_coverage"])
    assert coverage["counts"]["call_resolved"] == 5
    assert coverage["counts"]["call_unresolved"] == 1


def test_no_cross_file_global_name_guess():
    _, _, calls = resolve_sources(
        {
            "core.py": "def helper(): return 1\n",
            "use.py": "def run(): return helper()\n",
        }
    )
    assert calls == set()


def test_nested_and_conditional_definitions_have_lexical_scopes():
    modules, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
def outer(flag):
 if flag:
  def inner(): return helper()
 return inner()
def elsewhere(): return inner()
def recurse(): return recurse()
"""
        }
    )
    assert calls == {
        ("outer.inner", "helper"),
        ("outer", "outer.inner"),
        ("recurse", "recurse"),
    }
    assert any(str(f.key).endswith("#outer.inner") for f in modules[0].functions)


def test_class_scope_does_not_leak_into_method_and_receiver_writes_block_edges():
    _, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
class Store:
 def helper(self): return 2
 def run(self): return helper() + self.helper()
 @staticmethod
 def static(self): return self.helper()
 def wrong_receiver(other, self): return self.helper()
class Mutated:
 def helper(self): return 3
 def patch(self, fn): self.helper = fn
 def run(self): return self.helper()
"""
        }
    )
    assert calls == {("Store.run", "helper"), ("Store.run", "Store.helper")}


def test_comprehension_and_lambda_scopes_do_not_create_false_outer_calls():
    _, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
def run(values):
 result = [helper() for helper in values]
 callback = lambda helper: helper()
 return helper()
"""
        }
    )
    assert calls == {("run", "helper")}


def test_global_nonlocal_reads_and_writes():
    _, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
def run():
 global helper
 return helper()
def outer():
 def local(): return 2
 def inner():
  nonlocal local
  return local()
 return inner()
"""
        }
    )
    assert calls == {
        ("run", "helper"),
        ("outer.inner", "outer.local"),
        ("outer", "outer.inner"),
    }
    _, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
def patch(fn):
 global helper
 helper = fn
def run(): return helper()
def outer():
 def local(): return 2
 def patch(fn):
  nonlocal local
  local = fn
 def run(): return local()
 return run()
"""
        }
    )
    assert calls == {("outer", "outer.run")}


@pytest.mark.parametrize(
    "source",
    [
        "def helper(): return 1\nfrom missing import *\ndef run(): return helper()",
        "@unknown\ndef helper(): return 1\ndef run(): return helper()",
        'def helper(): return 1\ndef run():\n exec("pass")\n return helper()',
    ],
)
def test_dynamic_and_decorated_targets_are_unresolved(source):
    modules, _, calls = resolve_sources({"core.py": source + "\n"})
    assert calls == set()
    assert (
        json.loads(modules[0].module.properties["code_coverage"])["status"] == "partial"
    )


def test_import_cycles_and_ambiguous_roots_terminate_without_a_target():
    _, _, calls = resolve_sources(
        {
            "a.py": "from .b import helper\ndef run(): return helper()\n",
            "b.py": "from .a import helper\n",
        }
    )
    assert calls == set()
    modules = [
        _parse_python(
            Path("/fixture") / root / "core.py",
            "def helper(): return 1\n",
            repo=root,
            rel_path="core.py",
            calls=True,
        )
        for root in ["one", "two"]
    ]
    modules.append(
        _parse_python(
            Path("/fixture/three/use.py"),
            "from core import helper\ndef run(): return helper()\n",
            repo="three",
            rel_path="use.py",
            calls=True,
        )
    )
    assert not [e for e in resolve(modules, calls=True) if e.type == "CALLS"]


def test_python_refresh_corrects_owned_edges_preserves_authored_and_updates_coverage(
    engine, tmp_path
):
    root = tmp_path / "pkg"
    root.mkdir()
    core = root / "core.py"
    use = root / "use.py"
    core.write_text("def helper(): return 1\n")
    use.write_text("from .core import helper as renamed\ndef run(): return renamed()\n")
    request = CodeIngestRequest(paths=[str(root)])
    ingest_code(engine, engine.config, request)
    source_key = f"{_repo_id(root)}:use.py#run"
    target_key = f"{_repo_id(root)}:core.py#helper"
    # A separate authored relationship must survive a resolution correction.
    upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="CALLS",
                    from_label="Function",
                    from_key=target_key,
                    to_label="Function",
                    to_key=source_key,
                    source="agent",
                )
            ]
        ),
    )
    core.write_text("def helper(): return 1\nhelper = None\n")
    result = ingest_code(engine, engine.config, request)
    assert result.edges_pruned == 1
    assert engine.execute(
        "MATCH (a)-[r:CALLS]->(b) RETURN a.id,b.id,r._code_owner"
    ).rows == [[target_key, source_key, ""]]
    coverage = json.loads(
        engine.execute("MATCH (m:Module {path:'use.py'}) RETURN m.code_coverage").rows[
            0
        ][0]
    )
    assert coverage["counts"]["call_unresolved"] == 1
    assert ingest_code(engine, engine.config, request).files_unchanged == 2
    core.write_text("def helper(): return 2\n")
    ingest_code(engine, engine.config, request)
    assert engine.execute("MATCH ()-[r:CALLS]->() RETURN count(r)").rows == [[2]]
    use.write_text(
        "from .core import helper as renamed\ndef run(renamed): return renamed()\n"
    )
    ingest_code(engine, engine.config, request)
    assert engine.execute("MATCH ()-[r:CALLS]->() RETURN r._source").rows == [["agent"]]


def test_parser_upgrade_removes_old_owned_false_edge_without_source_edit(
    engine, tmp_path, monkeypatch
):
    from grag.ingest import code

    root = tmp_path / "pkg"
    root.mkdir()
    file = root / "core.py"
    file.write_text("def helper(): return 1\ndef run(helper): return helper()\n")
    request = CodeIngestRequest(paths=[str(root)])
    ingest_code(engine, engine.config, request)
    source = f"{_repo_id(root)}:core.py"
    engine.execute_write(
        "MATCH (a:Function {id:$a}), (b:Function {id:$b}) CREATE (a)-[:CALLS {_source:$source,_code_owner:$source}]->(b)",
        {"a": source + "#run", "b": source + "#helper", "source": str(file)},
    )
    monkeypatch.setattr(code, "PARSER_REVISION", code.PARSER_REVISION + "-upgrade-test")
    assert ingest_code(engine, engine.config, request).edges_pruned == 1
    assert engine.execute("MATCH ()-[r:CALLS]->() RETURN count(r)").rows == [[0]]


def test_nested_global_directive_overrides_an_outer_local():
    _, _, calls = resolve_sources(
        {
            "core.py": """def helper(): return 1
def outer():
 def helper(): return 2
 def inner():
  global helper
  def nested(): return helper()
  return nested()
 return inner()
"""
        }
    )
    assert ("outer.inner.nested", "helper") in calls
    assert ("outer.inner.nested", "outer.helper") not in calls


def test_call_site_cap_reports_the_full_unique_count():
    source = "def helper(): return 1\ndef run():\n" + "".join(
        " helper()\n" for _ in range(40)
    )
    _, edges, _ = resolve_sources({"core.py": source})
    sites = json.loads(next(e for e in edges if e.type == "CALLS").properties["sites"])
    assert sites["total"] == 40
    assert len(sites["lines"]) == 32


def test_namespace_child_imports_and_regular_module_boundaries():
    sources = {
        "pkg/core.py": "def helper(): return 1\n",
        "use.py": "from pkg import core\ndef run(): return core.helper()\n",
        "pkg/other.py": "from . import core\ndef local(): return core.helper()\n",
    }
    assert resolve_sources(sources)[2] == {("run", "helper"), ("local", "helper")}
    sources["pkg.py"] = "def unrelated(): return 0\n"
    assert resolve_sources(sources)[2] == set()
