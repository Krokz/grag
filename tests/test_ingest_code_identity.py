"""M36: stable overload sites, non-destructive legacy records and rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")
pytest.importorskip("tree_sitter_c_sharp")

from grag.core.mutate import define_schema, upsert_nodes
from grag.core.revisions import content_revision
from grag.core.types import (
    CodeIngestRequest,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.ingest import code, code_identity

JAVA = """class Store {
  Store() {}
  Store(int value) {}
  String helper(String value) { return value; }
  String helper(int value) { return "int"; }
  <T> T generic(T value) { return value; }
  <T,U> T generic(T value) { return value; }
  void many(String... values) {}
  void arrays(int[] values) {}
  void arrays(String values[]) {}
}
"""
CSHARP = """class Store {
  static Store() {}
  public Store() {}
  public Store(int value) {}
  public string Helper(string value) => value;
  public string Helper(int value) => "int";
  public T Generic<T>(T value) => value;
  public T Generic<T,U>(T value) => value;
  public void Change(ref int value) {}
  public void Change(int value) {}
  void IOne.Run() {}
  void ITwo.Run() {}
}
"""


def parsed(source, suffix):
    return code._PARSERS[suffix](
        Path("/fixture/Store" + suffix),
        source,
        repo="pkg",
        rel_path="Store" + suffix,
        calls=True,
    )


@pytest.mark.parametrize(
    ("suffix", "source", "count"), [(".java", JAVA, 9), (".cs", CSHARP, 11)]
)
def test_parameter_signatures_distinguish_overloads_and_stay_stable(
    suffix, source, count
):
    before = parsed(source, suffix)
    assert len(before.functions) == count
    ids = {fn.key for fn in before.functions}
    assert len(ids) == count
    # Formatting, local parameter names, comments and bodies do not identify an overload.
    edited = "// shifted\n\n" + source.replace("value", "renamed").replace(
        "(int renamed)", "(\n int /* note */ renamed\n)"
    ).replace('"int"', '"changed body"')
    after = parsed(edited, suffix)
    assert {fn.key for fn in after.functions} == ids
    assert all(
        json.loads(fn.properties["identity_signature"])[0] == "overload-v1"
        for fn in after.functions
    )
    assert all(len(str(fn.key).rsplit("~", 1)[1]) == 24 for fn in after.functions)
    assert all(fn.properties["line_start"] >= 3 for fn in after.functions)


@pytest.mark.parametrize(
    ("suffix", "source"),
    [
        (".java", "class Store { void helper(@A String value) {} }"),
        (".cs", 'class Store { void Helper([A] string value = "x") {} }'),
    ],
)
def test_parameter_annotations_and_defaults_do_not_change_identity(suffix, source):
    stripped = source.replace("@A ", "").replace("[A] ", "").replace(' = "x"', ' = "y"')
    assert (
        parsed(source, suffix).functions[0].key
        == parsed(stripped, suffix).functions[0].key
    )


@pytest.mark.parametrize(
    ("suffix", "source", "name"),
    [
        (
            ".java",
            'class Store {\n String helper(String x) { return x; }\n String helper(int x) { return ""; }\n}\n',
            "helper",
        ),
        (
            ".cs",
            'class Store {\n string Helper(string x) => x;\n string Helper(int x) => "";\n}\n',
            "Helper",
        ),
    ],
)
def test_persisted_overloads_survive_adding_removing_and_shifting(
    engine, tmp_path, suffix, source, name
):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / ("Store" + suffix)
    path.write_text(source)
    request = CodeIngestRequest(paths=[str(root)])
    assert code.ingest_code(engine, engine.config, request).functions == 2
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id,f.signature,f.line_start ORDER BY f.line_start"
    ).rows
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert [r[2] for r in rows] == [2, 3]
    assert engine.execute(
        "MATCH (:Class)-[:CONTAINS_CLASS_FUNCTION]->(f:Function) RETURN count(f)"
    ).rows == [[2]]
    assert code.ingest_code(engine, engine.config, request).files_unchanged == 1
    # Removing one overload cannot re-key the remaining declaration.
    path.write_text(
        "// moved\n" + source.splitlines()[0] + "\n" + source.splitlines()[1] + "\n}\n"
    )
    assert code.ingest_code(engine, engine.config, request).nodes_pruned == 1
    assert engine.execute("MATCH (f:Function) RETURN f.id,f.line_start").rows == [
        [rows[0][0], 3]
    ]
    path.write_text("\n\n" + source)
    code.ingest_code(engine, engine.config, request)
    assert {r[0] for r in engine.execute("MATCH (f:Function) RETURN f.id").rows} == {
        r[0] for r in rows
    }


def legacy_ingest(engine, root, monkeypatch):
    real = code_identity.function_identity

    def legacy(node, src, base):
        _, _, signature = real(node, src, base)
        return base, None, signature

    with monkeypatch.context() as context:
        context.setattr(code_identity, "function_identity", legacy)
        context.setattr(code_identity, "retire_legacy_functions", lambda *args: None)
        context.setattr(code, "PARSER_REVISION", "legacy-overload-test")
        return code.ingest_code(
            engine, engine.config, CodeIngestRequest(paths=[str(root)])
        )


def test_legacy_migration_keeps_authored_records_links_and_history(
    engine, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "Store.java"
    path.write_text(
        'class Store {\n String helper(String x) {return x;}\n String helper(int x) {return "";}\n}\n'
    )
    assert (
        legacy_ingest(engine, root, monkeypatch).functions == 1
    )  # reproduce the old collapse
    legacy = engine.execute("MATCH (f:Function) RETURN f.id").rows[0][0]
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(name="Note", properties=[PropertySpec(name="body")])
            ],
            rel_tables=[
                RelTableSpec(name="REMEMBERS", from_label="Note", to_label="Function")
            ],
        ),
    )
    authored = UpsertNodesRequest(
        operation_id="overload-authored-memory",
        nodes=[
            UpsertNode(
                label="Function",
                key=legacy,
                expected_revision=content_revision(
                    engine.execute(
                        "MATCH (f:Function {id:$id}) RETURN f", {"id": legacy}
                    ).rows[0][0]
                ),
                properties={
                    "docstring": "Author context: overload meaning needs review."
                },
                source="agent-authored provenance for the legacy symbol",
                evidence={"actor": "agent"},
            ),
            UpsertNode(
                label="Note",
                key="memory",
                properties={
                    "body": "An authored reference must not silently switch overload."
                },
            ),
        ],
        edges=[
            UpsertEdge(
                type="REMEMBERS",
                from_label="Note",
                from_key="memory",
                to_label="Function",
                to_key=legacy,
                source="agent",
            )
        ],
    )
    upsert_nodes(engine, engine.config, authored)
    before = engine.execute(
        "MATCH (f:Function {id:$id}) RETURN f.docstring,f._evidence_seq", {"id": legacy}
    ).rows
    request = CodeIngestRequest(paths=[str(root)])
    result = code.ingest_code(engine, engine.config, request)
    assert result.functions == 2
    assert any("overload identity migration" in warning for warning in result.warnings)
    assert engine.execute(
        "MATCH (f:Function {id:$id}) RETURN f._source", {"id": legacy}
    ).rows == [["agent-authored provenance for the legacy symbol"]]
    assert (
        engine.execute(
            "MATCH (f:Function {id:$id}) RETURN f.docstring,f._evidence_seq",
            {"id": legacy},
        ).rows
        == before
    )
    assert engine.execute(
        "MATCH (f:Function {id:$id}) RETURN f._source_state", {"id": legacy}
    ).rows == [["obsolete"]]
    assert engine.execute(
        "MATCH (:Note)-[r:REMEMBERS]->(f:Function) RETURN f.id,r._source"
    ).rows == [[legacy, "agent"]]
    assert engine.execute(
        "MATCH (:Class)-[:CONTAINS_CLASS_FUNCTION]->(f:Function) RETURN count(f)"
    ).rows == [[2]]
    assert engine.execute(
        "MATCH (f:Function) WHERE f.id STARTS WITH $prefix AND f._source_state='current' RETURN count(f)",
        {"prefix": legacy + "~"},
    ).rows == [[2]]
    assert upsert_nodes(engine, engine.config, authored).replayed
    # Even without an edge, the legacy record may contain authored properties/history.
    engine.execute_write("MATCH ()-[r:REMEMBERS]->() DELETE r")
    path.write_text("class Store {}\n")
    code.ingest_code(engine, engine.config, request)
    assert engine.execute(
        "MATCH (f:Function) RETURN f.id,f.docstring,f._evidence_seq"
    ).rows == [[legacy, *before[0]]]
    assert engine.execute("MATCH (n:Note) RETURN n.body").rows == [
        ["An authored reference must not silently switch overload."]
    ]


def test_migration_failure_rolls_back_retirement_and_new_identities(
    engine, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Store.cs").write_text(
        "class Store { void Run(int x) {} void Run(string x) {} }"
    )
    legacy_ingest(engine, root, monkeypatch)
    before = engine.execute(
        "MATCH (f:Function) RETURN f.id,f._source_state,f.signature"
    ).rows

    def fail(*args, **kwargs):
        raise RuntimeError("publication fault")

    with monkeypatch.context() as context:
        context.setattr(code, "_record_ingest_hashes", fail)
        with pytest.raises(RuntimeError, match="publication fault"):
            code.ingest_code(
                engine, engine.config, CodeIngestRequest(paths=[str(root)])
            )
    assert (
        engine.execute(
            "MATCH (f:Function) RETURN f.id,f._source_state,f.signature"
        ).rows
        == before
    )
    result = code.ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(root)])
    )
    assert result.functions == 2
    assert engine.execute(
        "MATCH (f:Function) WHERE f._source_state='current' RETURN count(f)"
    ).rows == [[2]]
