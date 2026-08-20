"""Polyglot code ingestion tests (Wave B): tree-sitter parsing of
TypeScript/JavaScript/C#/Terraform/Go into the same Repo/Module/Class/Function
graph as Python — correct language props and <repo-id>:<path> / #qualname ids,
CONTAINS_* edges, path/namespace/module-dir/package-name IMPORTS resolution,
and the ConfigurationError hint when the `code` extra is missing. The whole
module skips gracefully when the tree-sitter grammars aren't importable."""

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
    "tree_sitter_go",
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
  source  = "./child"
  version = "1.2.0"
}

module "datadog_alerts" {
  source  = "gitlab.com/yumbrands/critical-alerts/datadog"
  version = "~> 4.5.3"
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
    assert resp.module_calls == 2
    rows = engine.execute("MATCH (m:Module) RETURN m.id, m.language ORDER BY m.id").rows
    assert rows == [
        [child, "hcl"],
        [main, "hcl"],
    ]
    # module source "./child" resolves to the single .tf file in child/
    assert _edge_pairs(engine, "IMPORTS") == {(main, child)}

    # Both module blocks — local and registry source alike — become
    # TerraformModuleCall nodes with name/source/version read straight off
    # the block, so a version pin is queryable instead of hand-typed.
    calls = engine.execute(
        "MATCH (m:TerraformModuleCall) RETURN m.id, m.name, m.source, m.version "
        "ORDER BY m.id"
    ).rows
    assert calls == [
        [f"{main}#child", "child", "./child", "1.2.0"],
        [
            f"{main}#datadog_alerts",
            "datadog_alerts",
            "gitlab.com/yumbrands/critical-alerts/datadog",
            "~> 4.5.3",
        ],
    ]
    # Registry source gets a node too (no scanned file to point at, so no
    # IMPORTS edge, but the version is still recorded and queryable).
    assert _edge_pairs(engine, "CONTAINS_MODULE_MODULECALL") == {
        (main, f"{main}#child"),
        (main, f"{main}#datadog_alerts"),
    }


def test_reingest_terraform_prunes_removed_module_call(engine, tmp_path):
    infra = tmp_path / "infra"
    (infra / "child").mkdir(parents=True)
    (infra / "main.tf").write_text(MAIN_TF, encoding="utf-8")
    (infra / "child" / "main.tf").write_text(
        'variable "name" {\n  default = "x"\n}\n', encoding="utf-8"
    )
    req = CodeIngestRequest(paths=[str(infra)])
    ingest_code(engine, engine.config, req)
    main = _mid(infra, "main.tf")
    removed_id = f"{main}#datadog_alerts"

    # Bump the version on the surviving block and drop the registry one —
    # a re-ingest should pick up the new version and prune the removed call.
    (infra / "main.tf").write_text(
        MAIN_TF.replace('version = "1.2.0"', 'version = "1.3.0"').split(
            '\nmodule "datadog_alerts"'
        )[0]
        + "\n",
        encoding="utf-8",
    )

    resp = ingest_code(engine, engine.config, req)

    assert resp.module_calls == 1
    assert engine.execute(
        "MATCH (m:TerraformModuleCall) WHERE m.id = $id RETURN count(m)",
        {"id": removed_id},
    ).rows == [[0]]
    row = engine.execute(
        "MATCH (m:TerraformModuleCall) WHERE m.id = $id RETURN m.version",
        {"id": f"{main}#child"},
    ).rows
    assert row == [["1.3.0"]]
    assert _edge_pairs(engine, "CONTAINS_MODULE_MODULECALL") == {
        (main, f"{main}#child")
    }


# --- go ------------------------------------------------------------------------------------------

GREETER_GO = '''package greeter

import (
	"fmt"
	str "strings"
)

// Greeter says hello to people.
type Greeter struct {
	Name string
}

// Greet returns a greeting for name.
func (g *Greeter) Greet(name string) string {
	return fmt.Sprintf("hi %s", str.ToUpper(name))
}

// Shout is a package-level helper, not a method.
func Shout(s string) string {
	return str.ToUpper(s)
}

// Speaker can greet someone.
type Speaker interface {
	Greet(name string) string
}

// Status is a named int, not a struct or interface — Go allows methods on
// any defined type (sort.Interface / Stringer enums are idiomatic).
type Status int

// String implements fmt.Stringer for Status.
func (s Status) String() string {
	return "ok"
}
'''


def test_ingest_go(engine, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "greeter.go").write_text(GREETER_GO, encoding="utf-8")

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))
    mid = _mid(pkg, "greeter.go")

    assert resp.warnings == []
    assert (resp.repos, resp.modules, resp.classes, resp.functions) == (1, 1, 3, 4)
    assert engine.execute("MATCH (m:Module) RETURN m.language").rows == [["go"]]

    # struct, interface AND a plain named type (Status) all become Class
    # nodes: any of them can be a method receiver in Go, and a receiver
    # must always resolve to a Class node or upsert_edges fails outright.
    classes = engine.execute(
        "MATCH (c:Class) RETURN c.id, c.name, c.docstring ORDER BY c.id"
    ).rows
    assert classes == [
        [f"{mid}#Greeter", "Greeter", "Greeter says hello to people."],
        [f"{mid}#Speaker", "Speaker", "Speaker can greet someone."],
        [
            f"{mid}#Status",
            "Status",
            (
                "Status is a named int, not a struct or interface — Go allows "
                "methods on\nany defined type (sort.Interface / Stringer enums "
                "are idiomatic)."
            ),
        ],
    ]

    # A pointer-receiver method is linked to its struct's Class node by
    # receiver type, same as any other language's CONTAINS_CLASS_FUNCTION —
    # and so is a value-receiver method on the non-struct Status type.
    # The interface's method set becomes a Function too (is_method=true).
    functions = engine.execute(
        "MATCH (f:Function) RETURN f.id, f.is_method, f.docstring ORDER BY f.id"
    ).rows
    assert functions == [
        [f"{mid}#Greeter.Greet", True, "Greet returns a greeting for name."],
        [f"{mid}#Shout", False, "Shout is a package-level helper, not a method."],
        [f"{mid}#Speaker.Greet", True, ""],
        [f"{mid}#Status.String", True, "String implements fmt.Stringer for Status."],
    ]
    assert _edge_pairs(engine, "CONTAINS_CLASS_FUNCTION") == {
        (f"{mid}#Greeter", f"{mid}#Greeter.Greet"),
        (f"{mid}#Speaker", f"{mid}#Speaker.Greet"),
        (f"{mid}#Status", f"{mid}#Status.String"),
    }
    assert _edge_pairs(engine, "CONTAINS_MODULE_FUNCTION") == {(mid, f"{mid}#Shout")}


def test_ingest_go_imports_resolve_by_package_name(engine, tmp_path):
    repo = tmp_path / "repo"
    (repo / "internal" / "widget").mkdir(parents=True)
    (repo / "internal" / "widget" / "widget.go").write_text(
        "package widget\n\nfunc New() int { return 1 }\n", encoding="utf-8"
    )
    (repo / "main.go").write_text(
        'package main\n\n'
        'import (\n'
        '\t"fmt"\n'
        '\t"github.com/myorg/myrepo/internal/widget"\n'
        ')\n\n'
        "func main() {\n\tfmt.Println(widget.New())\n}\n",
        encoding="utf-8",
    )

    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(repo)]))
    main = _mid(repo, "main.go")
    widget = _mid(repo, "internal/widget/widget.go")

    assert resp.warnings == []
    # "fmt" (stdlib) has no local match and skips silently; the local
    # "github.com/myorg/myrepo/internal/widget" import resolves by matching
    # its last path segment against the target file's declared package name.
    assert _edge_pairs(engine, "IMPORTS") == {(main, widget)}


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
