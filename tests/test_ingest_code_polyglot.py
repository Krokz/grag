"""Polyglot code ingestion tests (Wave B): tree-sitter parsing of
TypeScript/JavaScript/C#/Terraform into the same Repo/Module/Class/Function
graph as Python — correct language props and <repo>:<path> / #qualname ids,
CONTAINS_* edges, path/namespace/module-dir IMPORTS resolution, and the
ConfigurationError hint when the `code` extra is missing. The whole module
skips gracefully when the tree-sitter grammars aren't importable."""

from __future__ import annotations

import sys

import pytest

from grag.core.errors import ConfigurationError
from grag.core.types import CodeIngestRequest
from grag.ingest.code import ingest_code

pytest.importorskip("tree_sitter", reason="code extra (tree-sitter) not installed")
for _grammar in (
    "tree_sitter_typescript",
    "tree_sitter_javascript",
    "tree_sitter_c_sharp",
    "tree_sitter_hcl",
):
    pytest.importorskip(_grammar, reason=f"{_grammar} not installed")


def _count(engine, cypher: str) -> int:
    return int(engine.execute(cypher).rows[0][0])


def _edge_pairs(engine, rel: str) -> set[tuple[str, str]]:
    rows = engine.execute(f"MATCH (a)-[r:{rel}]->(b) RETURN a.id, b.id").rows
    return {(r[0], r[1]) for r in rows}


# --- typescript -----------------------------------------------------------------------

HELPER_TS = """// Helper utilities.

/** Format a greeting. */
export function format(name: string): string {
  return `hi ${name}`;
}

export interface Speaker {
  speak(): void;
}
"""

GREETER_TS = """import { format } from "./helper";

/** Greeter class. */
export class Greeter implements Speaker {
  /** Say hello. */
  speak(): void {
    format("x");
  }
}

const arrow = async (n: number): Promise<number> => n;
"""


def test_ingest_typescript(engine, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "helper.ts").write_text(HELPER_TS, encoding="utf-8")
    (web / "greeter.ts").write_text(GREETER_TS, encoding="utf-8")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(web)]))

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 2, 4)

    rows = engine.execute(
        "MATCH (m:Module) RETURN m.id, m.name, m.language ORDER BY m.id"
    ).rows
    assert rows == [
        ["web:greeter.ts", "greeter", "typescript"],
        ["web:helper.ts", "helper", "typescript"],
    ]

    # interface Speaker is a Class node; Greeter's method hangs off the class
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == ["web:greeter.ts#Greeter", "web:helper.ts#Speaker"]
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id"
    ).rows
    assert [r[0] for r in rows] == [
        "web:greeter.ts#Greeter.speak",
        "web:greeter.ts#arrow",  # const arrow = ... is a module function
        "web:helper.ts#Speaker.speak",  # interface method
        "web:helper.ts#format",
    ]
    assert [r[1] for r in rows] == [True, False, True, False]

    # structure-only props: signature, leading comment as docstring, lines
    row = engine.execute(
        "MATCH (f:Function) WHERE f.id = 'web:helper.ts#format' "
        "RETURN f.signature, f.docstring, f.line_start, f.line_end, f.path"
    ).rows[0]
    assert row[0] == "function format(name: string): string"
    assert row[1] == "Format a greeting."
    assert row[2] == 4 and row[3] == 6
    assert row[4] == "helper.ts"
    row = engine.execute(
        "MATCH (c:Class) WHERE c.id = 'web:greeter.ts#Greeter' "
        "RETURN c.signature, c.docstring, c.language"
    ).rows[0]
    assert row[0] == "class Greeter implements Speaker"
    assert row[1] == "Greeter class."
    assert row[2] == "typescript"

    assert _edge_pairs(engine, "CONTAINS_MODULE_CLASS") == {
        ("web:greeter.ts", "web:greeter.ts#Greeter"),
        ("web:helper.ts", "web:helper.ts#Speaker"),
    }
    assert _edge_pairs(engine, "CONTAINS_CLASS_FUNCTION") == {
        ("web:greeter.ts#Greeter", "web:greeter.ts#Greeter.speak"),
        ("web:helper.ts#Speaker", "web:helper.ts#Speaker.speak"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_FUNCTION") == {
        ("web:greeter.ts", "web:greeter.ts#arrow"),
        ("web:helper.ts", "web:helper.ts#format"),
    }
    # relative import './helper' resolves path-based within the scanned set
    assert _edge_pairs(engine, "IMPORTS") == {("web:greeter.ts", "web:helper.ts")}
    # CALLS/INHERITS are Python-only in Wave B
    assert _count(engine, "MATCH ()-[r:CALLS]->() RETURN count(*)") == 0
    assert _count(engine, "MATCH ()-[r:INHERITS]->() RETURN count(*)") == 0


# --- javascript -------------------------------------------------------------------------

LEGACY_JS = """const { helper } = require("./helper");

function run(x) {
  return helper(x);
}

class App {
  start() {}
}
"""


def test_ingest_javascript_require(engine, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "legacy.js").write_text(LEGACY_JS, encoding="utf-8")
    (app / "helper.js").write_text(
        "module.exports = { helper: (s) => s };\n", encoding="utf-8"
    )

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(app)]))

    assert resp.warnings == []
    assert (resp.modules, resp.classes, resp.functions) == (2, 1, 2)
    rows = engine.execute("MATCH (m:Module) RETURN m.language").rows
    assert {r[0] for r in rows} == {"javascript"}
    rows = engine.execute("MATCH (f:Function) RETURN f.id ORDER BY f.id").rows
    assert [r[0] for r in rows] == ["app:legacy.js#App.start", "app:legacy.js#run"]
    # CommonJS require("./helper") resolves to the scanned helper.js module
    assert _edge_pairs(engine, "IMPORTS") == {("app:legacy.js", "app:helper.js")}


# --- c# -----------------------------------------------------------------------------------

WIDGET_CS = """using System;

namespace MyApp.Models;

/// A widget.
public class Widget
{
    /// Run it.
    public void Run() {}
}

public record WidgetConfig(string Name);
"""

GREETER_CS = """using MyApp.Models;

namespace MyApp.Services
{
    public interface IGreeter
    {
        void Greet(string name);
    }

    public class Greeter
    {
        public void Greet(string name) {}
    }
}
"""


def test_ingest_csharp(engine, tmp_path):
    svc = tmp_path / "svc"
    (svc / "Models").mkdir(parents=True)
    (svc / "Services").mkdir()
    (svc / "Models" / "Widget.cs").write_text(WIDGET_CS, encoding="utf-8")
    (svc / "Services" / "Greeter.cs").write_text(GREETER_CS, encoding="utf-8")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(svc)]))

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 4, 3)

    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.language ORDER BY m.id").rows
    assert rows == [
        ["svc:Models/Widget.cs", "csharp"],
        ["svc:Services/Greeter.cs", "csharp"],
    ]
    # qualnames are namespace-qualified (file-scoped and block forms alike)
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == [
        "svc:Models/Widget.cs#MyApp.Models.Widget",
        "svc:Models/Widget.cs#MyApp.Models.WidgetConfig",
        "svc:Services/Greeter.cs#MyApp.Services.Greeter",
        "svc:Services/Greeter.cs#MyApp.Services.IGreeter",
    ]
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id"
    ).rows
    assert [r[0] for r in rows] == [
        "svc:Models/Widget.cs#MyApp.Models.Widget.Run",
        "svc:Services/Greeter.cs#MyApp.Services.Greeter.Greet",
        "svc:Services/Greeter.cs#MyApp.Services.IGreeter.Greet",
    ]
    assert [r[1] for r in rows] == [True, True, True]

    # /// xmldoc becomes the docstring, tags stripped
    row = engine.execute(
        "MATCH (c:Class) WHERE c.id = 'svc:Models/Widget.cs#MyApp.Models.Widget' "
        "RETURN c.signature, c.docstring"
    ).rows[0]
    assert row[0] == "public class Widget"
    assert row[1] == "A widget."

    # `using MyApp.Models;` resolves to the file declaring that namespace
    assert _edge_pairs(engine, "IMPORTS") == {
        ("svc:Services/Greeter.cs", "svc:Models/Widget.cs")
    }


# --- terraform --------------------------------------------------------------------------------

MAIN_TF = """module "child" {
  source = "./child"
}

resource "aws_s3_bucket" "data" {
  bucket = "x"
}
"""


def test_ingest_terraform(engine, tmp_path):
    infra = tmp_path / "infra"
    (infra / "child").mkdir(parents=True)
    (infra / "main.tf").write_text(MAIN_TF, encoding="utf-8")
    (infra / "child" / "main.tf").write_text(
        'variable "name" {\n  default = "x"\n}\n', encoding="utf-8"
    )

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(infra)]))

    assert resp.warnings == []
    # HCL has no class/function declarations: Module nodes + IMPORTS only
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 0, 0)
    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.language ORDER BY m.id").rows
    assert rows == [
        ["infra:child/main.tf", "hcl"],
        ["infra:main.tf", "hcl"],
    ]
    # module source "./child" resolves to the single .tf file in child/
    assert _edge_pairs(engine, "IMPORTS") == {("infra:main.tf", "infra:child/main.tf")}


# --- error handling ----------------------------------------------------------------------------


def test_missing_code_extra_raises_configuration_error(engine, tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.ts").write_text("export function f(): void {}\n", encoding="utf-8")
    # Simulate a base install: importing tree_sitter fails.
    monkeypatch.setitem(sys.modules, "tree_sitter", None)

    with pytest.raises(ConfigurationError) as excinfo:
        ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    assert 'pip install "gragdb[code]"' in str(excinfo.value)


def test_tree_sitter_syntax_error_warns_and_skips(engine, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "broken.ts").write_text("class {\n", encoding="utf-8")
    (pkg / "ok.py").write_text("def f():\n    pass\n", encoding="utf-8")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    assert resp.modules == 1  # broken.ts skipped, ok.py ingested
    assert any("broken.ts" in w and "could not parse" in w for w in resp.warnings)
