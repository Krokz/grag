"""Useful Go navigation and refusal to invent dynamic/package/type relationships."""

from __future__ import annotations

import json

import pytest

from grag.code_state import scan_sources
from grag.core.types import CodeIngestRequest
from grag.ingest.code import _repo_id, ingest_code

pytest.importorskip("tree_sitter_go")


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def ingest(engine, root, **kwargs):
    return ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(root)], **kwargs)
    )


def calls(engine):
    return {
        tuple(row)
        for row in engine.execute(
            "MATCH (a:Function)-[r:CALLS]->(b:Function) RETURN a.name,b.name,r.resolution"
        ).rows
    }


def coverage(engine, path):
    return json.loads(
        engine.execute(
            "MATCH (m:Module {path:$path}) RETURN m.code_coverage", {"path": path}
        ).rows[0][0]
    )


def test_package_calls_receivers_interfaces_and_constant_citations(engine, tmp_path):
    write(tmp_path, "go.mod", "module example.com/graph\n")
    write(
        tmp_path,
        "store/types.go",
        """package store
// Reader reads the stored value.
type Reader interface { Load(key string) (int,error) }
type Store struct { value int }
// Limit is the maximum accepted work.
const Limit = 32 * 1024
""",
    )
    write(
        tmp_path,
        "store/methods.go",
        """package store
func (s *Store) Load(key string) (int,error) { s.save(); return 1,nil }
func (s Store) save() {}
func Helper() {}
func Run(s Store, r Reader) { Helper(); s.Load("key"); r.Load("key") }
""",
    )
    write(
        tmp_path,
        "main.go",
        """package main
import renamed "example.com/graph/store"
func main() { renamed.Helper() }
""",
    )
    result = ingest(engine, tmp_path)
    assert result.constants == 1
    assert calls(engine) == {
        ("Load", "save", "go_static_receiver"),
        ("Run", "Helper", "go_package_function"),
        ("Run", "Load", "go_static_receiver"),
        ("Run", "Load", "go_interface_method"),
        ("main", "Helper", "go_imported_function"),
    }
    assert engine.execute(
        "MATCH (a:Class)-[r:IMPLEMENTS_INTERFACE]->(b:Class) RETURN a.name,b.name,r.method_set"
    ).rows == [["Store", "Reader", "pointer"]]
    assert engine.execute(
        "MATCH (:Class)-[:INHERITS]->(:Class) RETURN count(*)"
    ).rows == [[0]]
    assert engine.execute(
        "MATCH (c:Constant) RETURN c.name,c.expression,c.path,c.line_start,c.docstring"
    ).rows == [
        [
            "Limit",
            "32 * 1024",
            "store/types.go",
            6,
            "Limit is the maximum accepted work.",
        ]
    ]
    assert engine.execute(
        "MATCH (a:Module {path:'main.go'})-[:IMPORTS]->(b:Module) RETURN b.path ORDER BY b.path"
    ).rows == [["store/methods.go"], ["store/types.go"]]
    site = engine.execute(
        "MATCH (a:Function {name:'main'})-[r:CALLS]->() RETURN r.sites"
    ).rows[0][0]
    assert json.loads(site) == {"lines": [[3, 3]], "total": 1}
    assert coverage(engine, "store/methods.go")["counts"]["calls_resolved"] == 4
    second = ingest(engine, tmp_path)
    assert second.files_unchanged == 3


def test_constants_grouped_iota_and_unevaluated_expressions(engine, tmp_path):
    write(
        tmp_path,
        "constants.go",
        """package sample
const (
    A, B int = 7, 9
    C = 1 << iota
    D
    _, F = 4, 5
)
const Text = `שלום`
""",
    )
    response = ingest(engine, tmp_path)
    assert response.constants == 6
    assert engine.execute(
        "MATCH (c:Constant) RETURN c.name,c.expression,c.declared_type,c.iota_index,c.inherited_expression,c.expression_line ORDER BY c.name"
    ).rows == [
        ["A", "7", "int", 0, False, 3],
        ["B", "9", "int", 0, False, 3],
        ["C", "1 << iota", "", 1, False, 4],
        ["D", "1 << iota", "", 2, True, 4],
        ["F", "5", "", 3, False, 6],
        ["Text", "`שלום`", "", 0, False, 8],
    ]
    assert coverage(engine, "constants.go")["counts"]["constants"] == 6
    write(tmp_path, "constants.go", "package sample\nconst A=8\n")
    ingest(engine, tmp_path)
    assert engine.execute("MATCH (c:Constant) RETURN c.name,c.expression").rows == [
        ["A", "8"]
    ]


def test_comments_do_not_shift_constant_values_or_receiver_bindings(engine, tmp_path):
    write(
        tmp_path,
        "a.go",
        """package /* package comment */ p
type Store struct{}
const A, B = 1, /* between values */ 2
func (/* receiver comment */ s * /* pointer comment */ Store) Load() {}
func Run() { s := /* value comment */ &Store{}; s.Load() }
""",
    )
    ingest(engine, tmp_path)
    assert engine.execute(
        "MATCH (c:Constant) RETURN c.name,c.expression ORDER BY c.name"
    ).rows == [["A", "1"], ["B", "2"]]
    assert calls(engine) == {("Run", "Load", "go_static_receiver")}
    assert engine.execute(
        "MATCH (t:Class)-[:CONTAINS_CLASS_FUNCTION]->(f:Function) RETURN t.name,f.name"
    ).rows == [["Store", "Load"]]
    assert coverage(engine, "a.go")["package"] == "p"


@pytest.mark.parametrize(
    "binding", ["func helper() {}", "type helper struct {}", "var helper func()"]
)
def test_local_shadowing_and_closures_never_become_package_calls(
    engine, tmp_path, binding
):
    # The package names are intentionally unique in the entire scanned tree;
    # the old Python global-name fallback must never be used for Go.
    local = "helper := func() {}" if binding.startswith("func") else binding
    write(
        tmp_path,
        "a.go",
        f"""package p
func helper() {{}}
func Run() {{ {local}; helper(); fn:=func(){{ helper() }}; _=fn }}
""",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == set()
    info = coverage(engine, "a.go")
    assert info["counts"]["calls_unresolved"] == 1
    assert info["counts"]["unindexed_closures"] >= 1


def test_alias_import_shadow_receiver_fields_and_function_variables(engine, tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "lib/lib.go", "package different\nfunc Load(){}\n")
    write(
        tmp_path,
        "a.go",
        """package p
import alias "example.com/x/lib"
type Store struct { Load func() }
func Load(){}
func Run(alias interface{}, fn func(), s Store) { alias.Load(); fn(); s.Load(); Load() }
""",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == {("Run", "Load", "go_package_function")}
    assert coverage(engine, "a.go")["counts"]["calls_unresolved"] == 3


def test_declared_package_name_and_external_basename_do_not_collide(engine, tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "lib/v2/one.go", "package store\nfunc Read(){}\n")
    write(tmp_path, "lib/v2/two.go", "package store\nfunc Save(){}\n")
    write(
        tmp_path,
        "main.go",
        """package main
import "example.com/x/lib/v2"
import fake "elsewhere.invalid/lib/v2"
func main(){ store.Read(); store.Save(); fake.Read() }
""",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == {
        ("main", "Read", "go_imported_function"),
        ("main", "Save", "go_imported_function"),
    }
    assert coverage(engine, "main.go")["counts"]["imports_unresolved"] == 1


def test_no_manifest_refuses_package_basename_guess(engine, tmp_path):
    write(tmp_path, "store/store.go", "package store\nfunc Read(){}\n")
    write(
        tmp_path,
        "main.go",
        'package main\nimport "external.invalid/store"\nfunc main(){store.Read()}\n',
    )
    ingest(engine, tmp_path)
    assert calls(engine) == set()
    assert engine.execute("MATCH ()-[:IMPORTS]->() RETURN count(*)").rows == [[0]]


def test_roots_and_external_test_packages_do_not_share_types(engine, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        write(root, "type.go", "package p\ntype Store struct{}\n")
        write(
            root,
            "method.go",
            "package p\nfunc(s Store) Load(){}\nfunc Run(s Store){s.Load()}\n",
        )
        write(root, "other_test.go", "package p_test\nfunc(s Store) Alien(){}\n")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(a), str(b)]))
    linked = engine.execute(
        "MATCH (c:Class)-[:CONTAINS_CLASS_FUNCTION]->(f:Function) RETURN c.id,f.id"
    ).rows
    assert len(linked) == 2
    for c, f in linked:
        assert c.split(":")[0] == f.split(":")[0]
        assert f.endswith("#Store.Load")
    for root in (a, b):
        assert any(c.startswith(_repo_id(root) + ":") for c, _ in linked)


def test_duplicate_build_variants_are_ambiguous(engine, tmp_path):
    write(
        tmp_path,
        "a_linux.go",
        "//go:build linux\npackage p\ntype Store struct{}\nfunc Load(){}\n",
    )
    write(
        tmp_path,
        "a_windows.go",
        "//go:build windows\npackage p\ntype Store struct{}\nfunc Load(){}\n",
    )
    write(
        tmp_path, "main.go", "package p\nfunc Run(){Load()}\nfunc(s Store) Read(){}\n"
    )
    ingest(engine, tmp_path)
    assert calls(engine) == set()
    assert engine.execute(
        "MATCH ()-[:CONTAINS_CLASS_FUNCTION]->() RETURN count(*)"
    ).rows == [[0]]
    assert coverage(engine, "a_linux.go")["build_constraints_present"]


def test_basic_method_sets_compare_types_variadic_and_pointer_semantics(
    engine, tmp_path
):
    write(
        tmp_path,
        "a.go",
        """package p
type Good interface { Run(a,b int, c ...string) (int,error) }
type Wrong interface { Run(string,int, ...string) (int,error) }
type Slice interface { Run(int,int, []string) (int,error) }
type Empty interface {}
type Embedded interface { Good }
type Generic[T any] interface { Run(T) }
type Alias = Good
type Store struct{}
func(s Store) Run(x,y int, z ...string) (int,error){return 0,nil}
""",
    )
    ingest(engine, tmp_path)
    assert engine.execute(
        "MATCH (a)-[r:IMPLEMENTS_INTERFACE]->(b) RETURN a.name,b.name,r.method_set"
    ).rows == [["Store", "Good", "value_and_pointer"]]
    assert coverage(engine, "a.go")["counts"]["types_unsupported"] >= 3


def test_method_signature_identical_spelling_is_not_cross_package_type_identity(
    engine, tmp_path
):
    write(
        tmp_path,
        "a/a.go",
        "package a\ntype Value int\ntype Reader interface { Load(Value) }\n",
    )
    write(
        tmp_path,
        "b/b.go",
        "package b\ntype Value int\ntype Store struct{}\nfunc(s Store) Load(Value){}\n",
    )
    ingest(engine, tmp_path)
    assert engine.execute(
        "MATCH ()-[:IMPLEMENTS_INTERFACE]->() RETURN count(*)"
    ).rows == [[0]]


def test_dependency_only_changes_reconcile_calls_and_method_sets(engine, tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "lib/lib.go", "package lib\nfunc Load(){}\n")
    write(
        tmp_path,
        "main.go",
        'package main\nimport "example.com/x/lib"\nfunc main(){lib.Load()}\n',
    )
    write(
        tmp_path,
        "types.go",
        "package main\ntype Reader interface { Read() int }\ntype Store struct{}\n",
    )
    write(tmp_path, "methods.go", "package main\nfunc(s Store) Read() int{return 1}\n")
    req = CodeIngestRequest(paths=[str(tmp_path)])
    ingest(engine, tmp_path)
    before = scan_sources(tmp_path, req)
    assert calls(engine)
    write(tmp_path, "go.mod", "module example.com/renamed\n")
    assert scan_sources(tmp_path, req).fingerprint != before.fingerprint
    ingest(engine, tmp_path)
    assert calls(engine) == set()
    assert coverage(engine, "main.go")["counts"]["calls_unresolved"] == 1
    write(
        tmp_path,
        "methods.go",
        'package main\nfunc(s Store) Read() string{return "x"}\n',
    )
    ingest(engine, tmp_path)
    assert engine.execute(
        "MATCH ()-[:IMPLEMENTS_INTERFACE]->() RETURN count(*)"
    ).rows == [[0]]
    write(tmp_path, "lib/lib.go", "package lib\nfunc Save(){}\n")
    write(tmp_path, "go.mod", "module example.com/x\n")
    ingest(engine, tmp_path)
    assert calls(engine) == set()
    write(tmp_path, "lib/lib.go", "package lib\nfunc Load(){}\n")
    ingest(engine, tmp_path)
    assert calls(engine) == {("main", "Load", "go_imported_function")}


def test_calls_disabled_preserves_constants_imports_and_coverage(engine, tmp_path):
    write(
        tmp_path, "a.go", "package p\nconst Max=4\nfunc Load(){}\nfunc Run(){Load()}\n"
    )
    ingest(engine, tmp_path)
    assert calls(engine)
    result = ingest(engine, tmp_path, calls=False)
    assert calls(engine) == set() and result.constants == 1
    assert coverage(engine, "a.go")["calls_enabled"] is False


def test_local_typed_and_literal_receivers_and_shadowed_scopes(engine, tmp_path):
    write(
        tmp_path,
        "a.go",
        """package p
type Store struct{}
func(s *Store) Load(){}
func Run(){ var s Store; s.Load(); p:=&Store{}; p.Load(); x:=Store{}; x.Load() }
func Shadow(){var s Store; if true {s:=func(){};s()};s.Load()}
""",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == {("Run", "Load", "go_static_receiver")}
    sites = json.loads(
        engine.execute(
            "MATCH (a:Function {name:'Run'})-[r:CALLS]->() RETURN r.sites"
        ).rows[0][0]
    )
    # Three calls on the same line have a single line citation, with honest unique-site count.
    assert sites == {"lines": [[4, 4]], "total": 1}


@pytest.mark.parametrize(
    "source",
    [
        "type Store struct{}; var s Store; s.Load()",
        "type Store = int; var s Store; s.Load()",
        "for Load := range []func(){} { Load() }",
        "switch Load := x.(type) { case func(): Load() }",
        "select { case Load := <-ch: Load() }",
    ],
)
def test_local_type_range_select_switch_shadowing(engine, tmp_path, source):
    write(
        tmp_path,
        "a.go",
        "package p\ntype Store struct{}\nfunc(s Store)Load(){}\nfunc Load(){}\nfunc Run(x interface{},ch chan func()){"
        + source
        + "}\n",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == set()


def test_constants_are_searchable_and_document_mentions_survive_reopen(
    engine, tmp_path
):
    from grag.core.engine import Engine
    from grag.core.types import IngestRequest, SearchRequest
    from grag.ingest.loaders import ingest_documents
    from grag.retrieval.search import search_knowledge

    write(
        tmp_path,
        "a.go",
        "package p\n// QuartzLimit controls retries.\nconst QuartzLimit = 83\n",
    )
    ingest(engine, tmp_path)
    request = IngestRequest.model_validate(
        {
            "documents": [
                {"text": "# Retry policy\n\nUse `QuartzLimit`.", "source": "policy.md"}
            ],
            "sections": True,
        }
    )
    ingest_documents(engine, engine.config, request)
    assert engine.execute(
        "MATCH (:Section)-[:MENTIONS_CONSTANT]->(c:Constant) RETURN c.name"
    ).rows == [["QuartzLimit"]]
    engine.close()
    with Engine(engine.config) as reopened:
        result = search_knowledge(
            reopened,
            reopened.config,
            SearchRequest(query="QuartzLimit", labels=["Constant"], hops=0),
        )
        assert result.included_node_ids and "83" in result.context
        assert "a.go" in result.context


def test_module_manifest_is_tracked_with_selected_files_and_invalid_utf8_is_actionable(
    engine, tmp_path
):
    from grag.core.errors import GragError

    write(tmp_path, "go.mod", "module example.com/x\n")
    file = write(tmp_path, "pkg/a.go", "package p\nfunc Load(){}\n")
    req = CodeIngestRequest(paths=[str(file)], root=str(tmp_path))
    ingest_code(engine, engine.config, req)
    scan = scan_sources(tmp_path, req)
    assert str(tmp_path / "go.mod") in scan.files
    assert set(scan.files) == {str(file), str(tmp_path / "go.mod")}
    (tmp_path / "go.mod").write_bytes(b"\xff")
    with pytest.raises(GragError, match="Cannot read"):
        scan_sources(tmp_path, req)


def test_symlinked_go_mod_is_refused_without_indexing_external_metadata(
    engine, tmp_path
):
    external = write(tmp_path, "outside.mod", "module outside.invalid/x\n")
    root = tmp_path / "root"
    write(root, "a.go", "package p\nfunc Run(){}\n")
    (root / "go.mod").symlink_to(external)
    result = ingest(engine, root)
    assert any("must not be a symlink" in warning for warning in result.warnings)
    assert result.modules == 0


def test_external_test_package_does_not_make_the_importable_package_ambiguous(
    engine, tmp_path
):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "store/store.go", "package store\nfunc Load(){}\n")
    write(
        tmp_path,
        "store/store_test.go",
        'package store_test\nimport "example.com/x/store"\nfunc Check(){store.Load()}\n',
    )
    write(
        tmp_path,
        "main.go",
        'package main\nimport "example.com/x/store"\nfunc main(){store.Load()}\n',
    )
    ingest(engine, tmp_path)
    assert calls(engine) == {
        ("Check", "Load", "go_imported_function"),
        ("main", "Load", "go_imported_function"),
    }


def test_variadic_receiver_argument_is_a_slice_not_an_element(engine, tmp_path):
    write(
        tmp_path,
        "a.go",
        "package p\ntype Store struct{}\nfunc(s Store)Load(){}\nfunc Run(s ...Store){s.Load()}\n",
    )
    ingest(engine, tmp_path)
    assert calls(engine) == set()
