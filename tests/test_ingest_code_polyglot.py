"""Polyglot code ingestion tests (Wave B): tree-sitter parsing of
TypeScript/JavaScript/C#/Terraform into the same Repo/Module/Class/Function
graph as Python — correct language props and <repo-id>:<path> / #qualname ids,
CONTAINS_* edges, path/namespace/module-dir IMPORTS resolution, and the
ConfigurationError hint when the `code` extra is missing. The whole module
skips gracefully when the tree-sitter grammars aren't importable."""

from __future__ import annotations

import sys

import pytest

from grag.core.errors import ConfigurationError
from grag.core.types import CodeIngestRequest
from grag.ingest.code import _repo_id, ingest_code

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


def _mid(root, relative: str) -> str:
    return f"{_repo_id(root)}:{relative}"


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
    greeter = _mid(web, "greeter.ts")
    helper = _mid(web, "helper.ts")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(web)]))

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 2, 4)

    rows = engine.execute(
        "MATCH (m:Module) RETURN m.id, m.name, m.language ORDER BY m.id"
    ).rows
    assert rows == [
        [greeter, "greeter", "typescript"],
        [helper, "helper", "typescript"],
    ]

    # interface Speaker is a Class node; Greeter's method hangs off the class
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == [f"{greeter}#Greeter", f"{helper}#Speaker"]
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id"
    ).rows
    assert [r[0] for r in rows] == [
        f"{greeter}#Greeter.speak",
        f"{greeter}#arrow",  # const arrow = ... is a module function
        f"{helper}#Speaker.speak",  # interface method
        f"{helper}#format",
    ]
    assert [r[1] for r in rows] == [True, False, True, False]

    # structure-only props: signature, leading comment as docstring, lines
    row = engine.execute(
        "MATCH (f:Function) WHERE f.id = $id "
        "RETURN f.signature, f.docstring, f.line_start, f.line_end, f.path",
        {"id": f"{helper}#format"},
    ).rows[0]
    assert row[0] == "function format(name: string): string"
    assert row[1] == "Format a greeting."
    assert row[2] == 4 and row[3] == 6
    assert row[4] == "helper.ts"
    row = engine.execute(
        "MATCH (c:Class) WHERE c.id = $id RETURN c.signature, c.docstring, c.language",
        {"id": f"{greeter}#Greeter"},
    ).rows[0]
    assert row[0] == "class Greeter implements Speaker"
    assert row[1] == "Greeter class."
    assert row[2] == "typescript"

    assert _edge_pairs(engine, "CONTAINS_MODULE_CLASS") == {
        (greeter, f"{greeter}#Greeter"),
        (helper, f"{helper}#Speaker"),
    }
    assert _edge_pairs(engine, "CONTAINS_CLASS_FUNCTION") == {
        (f"{greeter}#Greeter", f"{greeter}#Greeter.speak"),
        (f"{helper}#Speaker", f"{helper}#Speaker.speak"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_FUNCTION") == {
        (greeter, f"{greeter}#arrow"),
        (helper, f"{helper}#format"),
    }
    # relative import './helper' resolves path-based within the scanned set
    assert _edge_pairs(engine, "IMPORTS") == {(greeter, helper)}
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
    legacy = _mid(app, "legacy.js")
    helper = _mid(app, "helper.js")

    assert resp.warnings == []
    assert (resp.modules, resp.classes, resp.functions) == (2, 1, 2)
    rows = engine.execute("MATCH (m:Module) RETURN m.language").rows
    assert {r[0] for r in rows} == {"javascript"}
    rows = engine.execute("MATCH (f:Function) RETURN f.id ORDER BY f.id").rows
    assert [r[0] for r in rows] == [f"{legacy}#App.start", f"{legacy}#run"]
    # CommonJS require("./helper") resolves to the scanned helper.js module
    assert _edge_pairs(engine, "IMPORTS") == {(legacy, helper)}


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
    widget = _mid(svc, "Models/Widget.cs")
    greeter = _mid(svc, "Services/Greeter.cs")

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 4, 3)

    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.language ORDER BY m.id").rows
    assert rows == [
        [widget, "csharp"],
        [greeter, "csharp"],
    ]
    # qualnames are namespace-qualified (file-scoped and block forms alike)
    rows = engine.execute("MATCH (c:Class) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == [
        f"{widget}#MyApp.Models.Widget",
        f"{widget}#MyApp.Models.WidgetConfig",
        f"{greeter}#MyApp.Services.Greeter",
        f"{greeter}#MyApp.Services.IGreeter",
    ]
    rows = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method ORDER BY f.id"
    ).rows
    assert [r[0] for r in rows] == [
        f"{widget}#MyApp.Models.Widget.Run",
        f"{greeter}#MyApp.Services.Greeter.Greet",
        f"{greeter}#MyApp.Services.IGreeter.Greet",
    ]
    assert [r[1] for r in rows] == [True, True, True]

    # /// xmldoc becomes the docstring, tags stripped
    row = engine.execute(
        "MATCH (c:Class) WHERE c.id = $id RETURN c.signature, c.docstring",
        {"id": f"{widget}#MyApp.Models.Widget"},
    ).rows[0]
    assert row[0] == "public class Widget"
    assert row[1] == "A widget."

    # `using MyApp.Models;` resolves to the file declaring that namespace
    assert _edge_pairs(engine, "IMPORTS") == {(greeter, widget)}


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
    child = _mid(infra, "child/main.tf")
    main = _mid(infra, "main.tf")

    assert resp.warnings == []
    # HCL has no class/function declarations: Module nodes + IMPORTS only
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 2, 0, 0)
    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.language ORDER BY m.id").rows
    assert rows == [
        [child, "hcl"],
        [main, "hcl"],
    ]
    # module source "./child" resolves to the single .tf file in child/
    assert _edge_pairs(engine, "IMPORTS") == {(main, child)}


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
