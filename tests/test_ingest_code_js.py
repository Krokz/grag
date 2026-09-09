"""Static JS/TS edges must be grounded in bindings, exports and original lines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grag.core.types import CodeIngestRequest
from grag.ingest.code import _repo_id, ingest_code
from grag.ingest.code_js import resolve
from grag.ingest.code_ts import parse_file

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")


def parsed(files, *, calls=True):
    modules = [
        parse_file(
            Path(path).suffix, Path("/repo") / path, text, repo="repo", rel_path=path
        )
        for path, text in files.items()
    ]
    edges = resolve(modules, calls=calls)
    return modules, edges


def pairs(edges, kind="CALLS"):
    return {
        (str(e.from_key).removeprefix("repo:"), str(e.to_key).removeprefix("repo:"))
        for e in edges
        if e.type == kind
    }


def coverage(module):
    return json.loads(module.module.properties["code_coverage"])


@pytest.mark.parametrize("ext", ["ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts"])
def test_local_calls_and_citations(ext):
    modules, edges = parsed(
        {
            f"main.{ext}": """function helper() {}
const arrow = () => helper();
function run() {
  helper();
  helper();
  arrow();
}
function recursive() { recursive(); }
"""
        }
    )
    path = f"main.{ext}"
    assert pairs(edges) == {
        (f"{path}#arrow", f"{path}#helper"),
        (f"{path}#run", f"{path}#helper"),
        (f"{path}#run", f"{path}#arrow"),
        (f"{path}#recursive", f"{path}#recursive"),
    }
    edge = next(
        e
        for e in edges
        if str(e.from_key).endswith("#run") and str(e.to_key).endswith("#helper")
    )
    assert edge.source == f"/repo/{path}"
    assert json.loads(edge.properties["sites"]) == {
        "lines": [[4, 4], [5, 5]],
        "total": 2,
    }
    assert coverage(modules[0])["counts"]["calls_resolved"] == 5


def test_import_aliases_default_namespace_and_named_reexports():
    modules, edges = parsed(
        {
            "lib.ts": "export function helper() {}\nexport default function primary() {}\nexport class Base {}",
            "barrel.ts": "export {helper as via, default as primary, Base} from './lib.js';",
            "main.ts": """import main, {helper as alias} from './lib.js';
import * as ns from './lib';
import {via, primary, Base} from './barrel';
export function run() { alias(); main(); ns.helper(); via(); primary(); }
export class Child extends Base { run() { alias(); } }
""",
        }
    )
    assert pairs(edges) == {
        ("main.ts#run", "lib.ts#helper"),
        ("main.ts#run", "lib.ts#primary"),
        ("main.ts#Child.run", "lib.ts#helper"),
    }
    assert pairs(edges, "INHERITS") == {("main.ts#Child", "lib.ts#Base")}
    assert coverage(modules[-1])["counts"]["calls_resolved"] == 6


@pytest.mark.parametrize(
    "body",
    [
        "function run(helper) { helper(); }",
        "function run({helper}) { helper(); }",
        "function run({a: helper}) { helper(); }",
        "function run([helper]) { helper(); }",
        "function run(...helper) { helper(); }",
        "function run(helper: () => void = fallback) { helper(); }",
        "function run() { helper(); let helper; }",
        "function run() { helper(); var helper; }",
        "function run() { { helper(); const helper = other; } }",
        "function run() { for (const helper of things) helper(); }",
        "function run() { for (var helper of things) {} helper(); }",
        "function run() { try {} catch(helper) { helper(); } }",
        "function run() { try {} catch({message: helper}) { helper(); } }",
        "function run() { function helper() {} helper(); }",
        "function run() { const alias = helper; alias(); }",
        "function run() { return obj.helper(); }",
        'function run() { return obj["helper"](); }',
        "function run() { helper.call(null); }",
        "function run() { const later = () => helper(); }",
        "function run() { [1].map(() => helper()); }",
        "function run() { const later = function helper() { helper(); }; }",
        "function run() { helper(); } helper = replacement;",
        "function run() { helper(); } [helper] = replacements;",
        "function run() { helper(); } ({a: helper} = replacement);",
        "function run() { helper(); } function mutate() { helper++; }",
        'function run() { helper(); eval("helper = other"); }',
        "namespace N { const helper = other; export function run() { helper(); } }",
    ],
)
def test_shadowing_mutation_and_dynamic_calls_never_guess(body):
    modules, edges = parsed({"main.ts": "function helper() {}\n" + body})
    assert pairs(edges) == set()
    assert coverage(modules[0])["mode"] == "partial_static"


def test_block_shadow_does_not_hide_sibling_call_and_closure_scope():
    _, edges = parsed(
        {
            "main.ts": """function helper() {}
function run() { { const helper = other; helper(); } helper(); }
namespace N { export function run() { helper(); } }
"""
        }
    )
    assert pairs(edges) == {
        ("main.ts#run", "main.ts#helper"),
        ("main.ts#N.run", "main.ts#helper"),
    }


@pytest.mark.parametrize(
    "import_code,lib",
    [
        ("import {helper} from './lib';", "function helper() {}"),
        ("import type {helper} from './lib';", "export function helper() {}"),
        ("import {type helper} from './lib';", "export function helper() {}"),
        ("import {helper} from 'lib';", "export function helper() {}"),
        ("const {helper} = require('./lib');", "export function helper() {}"),
        ("", "export function helper() {}"),
        ("import {helper} from './lib';", 'export * from "./actual";'),
        ("import {helper} from './lib';", 'export {helper} from "./lib";'),
        (
            "import {helper} from './lib';",
            "export let helper = () => {}; helper = other;",
        ),
        ("import helper from './lib';", "export default () => {};"),
    ],
)
def test_unresolved_imports_do_not_fall_back_to_global_names(import_code, lib):
    _, edges = parsed(
        {
            "lib.ts": lib,
            "actual.ts": "export function helper() {}",
            "main.ts": import_code + "\nfunction run() { helper(); }",
        }
    )
    assert pairs(edges) == set()


def test_ambiguous_extensions_and_index_file_are_not_guessed():
    _, edges = parsed(
        {
            "lib.ts": "export function helper() {}",
            "lib.js": "export function helper() {}",
            "lib/index.ts": "export function helper() {}",
            "main.ts": "import {helper} from './lib'; function run() {helper();}",
        }
    )
    assert pairs(edges) == set()
    assert pairs(edges, "IMPORTS") == set()


def test_relative_resolution_is_root_scoped():
    first = parse_file(
        ".ts",
        Path("/one/main.ts"),
        "import {helper} from './lib'; function run() {helper();}",
        repo="one",
        rel_path="main.ts",
    )
    second = parse_file(
        ".ts",
        Path("/two/lib.ts"),
        "export function helper() {}",
        repo="two",
        rel_path="lib.ts",
    )
    escaped = parse_file(
        ".ts",
        Path("/one/escape.ts"),
        "import {helper} from '../two/lib'; function run() {helper();}",
        repo="one",
        rel_path="escape.ts",
    )
    edges = resolve([first, second, escaped], calls=True)
    assert not edges


def test_namespace_mutation_and_shadowing():
    _, edges = parsed(
        {
            "lib.ts": "export function helper() {}",
            "main.ts": """import * as ns from './lib';
function shadow(ns) { ns.helper(); }
function run() { ns.helper(); }
ns.helper = replacement;
""",
        }
    )
    assert pairs(edges) == set()


@pytest.mark.parametrize("ext", ["ts", "js"])
def test_runtime_extends_only(ext):
    _, edges = parsed(
        {
            f"main.{ext}": """class Base {}
class Child extends Base {}
class Dynamic extends mixin(Base) {}
function factory(Base) { class Inner extends Base {} }
"""
        }
    )
    assert pairs(edges, "INHERITS") == {(f"main.{ext}#Child", f"main.{ext}#Base")}


def test_type_only_inheritance_is_visible():
    modules, edges = parsed(
        {
            "main.ts": "interface Base {}\ninterface Child extends Base {}\nclass Impl implements Base {}"
        }
    )
    assert pairs(edges, "INHERITS") == set()
    assert coverage(modules[0])["counts"]["type_relationship"] == 2


def test_calls_disabled_keeps_definitions_imports_and_extends():
    modules, edges = parsed(
        {
            "main.ts": "class Base {}\nclass Child extends Base {}\nfunction run() {run();}"
        },
        calls=False,
    )
    assert not pairs(edges)
    assert len(modules[0].functions) == 1
    assert pairs(edges, "INHERITS") == {("main.ts#Child", "main.ts#Base")}
    assert coverage(modules[0])["calls_enabled"] is False


def test_svelte_blocks_keep_offsets_and_do_not_cross_scopes():
    modules, edges = parsed(
        {
            "Page.svelte": """<!-- <script>function fake() {}</script> -->
<h1>é example</h1>
<script context="module" lang="ts">
export function helper() {}
export function run() { helper(); }
</script>
<script lang="ts">
function run() { helper(); }
const click = () => run();
</script>
<button onclick={click}>go</button>
"""
        }
    )
    pm = modules[0]
    assert {n.key for n in pm.functions} == {
        "repo:Page.svelte#module.helper",
        "repo:Page.svelte#module.run",
        "repo:Page.svelte#instance.run",
        "repo:Page.svelte#instance.click",
    }
    assert pairs(edges) == {
        ("Page.svelte#module.run", "Page.svelte#module.helper"),
        ("Page.svelte#instance.click", "Page.svelte#instance.run"),
    }
    assert [
        (n.properties["name"], n.properties["line_start"]) for n in pm.functions
    ] == [("helper", 4), ("run", 5), ("run", 8), ("click", 9)]
    assert pm.module.properties["language"] == "svelte"
    assert all(n.properties["language"] == "typescript" for n in pm.functions)
    assert coverage(pm)["counts"]["calls_unresolved"] == 1


def test_astro_frontmatter_and_browser_scripts_are_separate():
    modules, edges = parsed(
        {
            "Page.astro": """---
import {helper} from './lib';
function server() { helper(); }
---
<div>{server()}</div>
<script>
function browser() { helper(); }
function local() { browser(); }
</script>
<script src="/external.js"></script>
<script type="application/ld+json">{"a":1}</script>
""",
            "lib.ts": "export function helper() {}",
        }
    )
    assert pairs(edges) == {
        ("Page.astro#frontmatter.server", "lib.ts#helper"),
        ("Page.astro#script_1.local", "Page.astro#script_1.browser"),
    }
    cov = coverage(modules[0])
    assert cov["scripts"] == 2
    assert cov["omitted"] == ["external_script", "unsupported_script_language_or_type"]
    assert [
        (n.properties["name"], n.properties["line_start"]) for n in modules[0].functions
    ] == [("server", 3), ("browser", 7), ("local", 8)]


def test_vue_reads_both_normal_and_setup_scripts():
    modules, edges = parsed(
        {
            "App.vue": """<template><pre>&lt;script&gt;</pre></template>
<script>function normal() {} function run() {normal();}</script>
<script setup lang='ts'>const setup = () => normal();</script>"""
        }
    )
    assert {n.properties["name"] for n in modules[0].functions} == {
        "normal",
        "run",
        "setup",
    }
    assert pairs(edges) == {("App.vue#run", "App.vue#normal")}
    assert coverage(modules[0])["scripts"] == 2


@pytest.mark.parametrize(
    "path,source",
    [
        ("Page.svelte", "<p>Template only</p>"),
        ("Page.astro", '<script lang="coffee">unknown code</script>'),
    ],
)
def test_empty_unsupported_framework_scope_is_visible(path, source):
    modules, edges = parsed({path: source})
    assert not edges and not modules[0].functions
    assert "no_supported_script" in coverage(modules[0])["omitted"]


@pytest.mark.parametrize(
    "path,source",
    [
        ("Page.svelte", "<script>function broken(</script>"),
        ("Page.astro", "---\nfunction broken() {}"),
        ("Page.svelte", "<script>function broken() {}"),
    ],
)
def test_bad_framework_script_does_not_claim_success(path, source):
    with pytest.raises(ValueError):
        parsed({path: source})


def test_citation_and_diagnostic_samples_are_bounded():
    modules, edges = parsed(
        {
            "main.ts": "function helper() {}\nfunction run() {\n"
            + "helper();\n" * 40
            + "unknown();\n" * 40
            + "}"
        }
    )
    sites = json.loads(edges[0].properties["sites"])
    assert len(sites["lines"]) == 32 and sites["total"] == 40
    assert len(coverage(modules[0])["examples"]) == 5
    assert coverage(modules[0])["counts"]["calls_unresolved"] == 40


def test_incremental_reexport_changes_remove_and_retarget_edges(engine, tmp_path):
    (tmp_path / "a.ts").write_text("export function helper() {}")
    (tmp_path / "b.ts").write_text("export function helper() {}")
    barrel = tmp_path / "barrel.ts"
    barrel.write_text("export {helper} from './a';")
    (tmp_path / "main.ts").write_text(
        "import {helper} from './barrel'; function run() {helper();}"
    )
    req = CodeIngestRequest(paths=[str(tmp_path)])
    first = ingest_code(engine, engine.config, req)
    assert first.files_parsed == 4
    stable = ingest_code(engine, engine.config, req)
    assert stable.files_unchanged == 4
    mid = f"{_repo_id(tmp_path)}:main.ts#run"

    def targets():
        return engine.execute(
            "MATCH (f:Function)-[r:CALLS]->(g:Function) WHERE f.id=$id RETURN g.path,r.sites",
            {"id": mid},
        ).rows

    assert targets()[0][0] == "a.ts"
    barrel.write_text("export {helper} from './b';")
    changed = ingest_code(engine, engine.config, req)
    assert changed.files_unchanged == 2  # barrel + derived caller rewritten
    assert targets()[0][0] == "b.ts"
    assert json.loads(targets()[0][1])["lines"] == [[1, 1]]
    barrel.write_text("export const value = 1;")
    ingest_code(engine, engine.config, req)
    assert targets() == []
    stored = engine.execute(
        'MATCH (m:Module) WHERE m.path="main.ts" RETURN m.code_coverage'
    ).rows[0][0]
    assert json.loads(stored)["counts"]["calls_unresolved"] == 1


def test_parser_revision_invalidates_freshness_and_file_cache(
    engine, tmp_path, monkeypatch
):
    from grag.code_state import scan_sources
    from grag.ingest import code

    (tmp_path / "main.ts").write_text("function run() {}")
    req = CodeIngestRequest(paths=[str(tmp_path)], root=str(tmp_path))
    ingest_code(engine, engine.config, req)
    before = scan_sources(tmp_path, req).fingerprint
    assert ingest_code(engine, engine.config, req).files_unchanged == 1
    monkeypatch.setattr(code, "PARSER_REVISION", "next-parser")
    assert scan_sources(tmp_path, req).fingerprint != before
    assert ingest_code(engine, engine.config, req).files_unchanged == 0


@pytest.mark.parametrize(
    "body",
    [
        "function run() { helper(); } (helper) = other;",
        "namespace N { import helper = Thing.helper; export function run() { helper(); } }",
        'namespace N { import helper = require("./other"); export function run() { helper(); } }',
    ],
)
def test_additional_assignment_and_ts_alias_shadows(body):
    _, edges = parsed({"main.ts": "function helper() {}\n" + body})
    assert pairs(edges) == set()


def test_destructured_namespace_write():
    _, edges = parsed(
        {
            "lib.ts": "export function helper() {}",
            "main.ts": """import * as ns from './lib';
function run() { ns.helper(); }
({x: ns.helper} = other);
""",
        }
    )
    assert pairs(edges) == set()


def test_computed_method_name_is_not_a_call_by_the_method():
    modules, edges = parsed(
        {"main.ts": "function computed() {}\nclass C { [computed()]() {} }"}
    )
    assert pairs(edges) == set()
    assert coverage(modules[0])["counts"]["unindexed_caller"] == 1


def test_same_line_unindexed_body_never_borrows_symbol_identity():
    _, edges = parsed(
        {
            "main.ts": "function helper() {} function run() {} const object = { run() { helper(); } };"
        }
    )
    assert pairs(edges) == set()


def test_resolution_uses_portable_repo_paths_not_host_path_separators():
    modules, _ = parsed(
        {
            "src/lib.ts": "export function helper() {}",
            "src/main.ts": "import {helper} from './lib.js'; function run() { helper(); }",
        }
    )
    for module in modules:
        module.module.source = "C:\\checkout\\" + module.module.properties[
            "path"
        ].replace("/", "\\")
    assert pairs(resolve(modules, calls=True)) == {
        ("src/main.ts#run", "src/lib.ts#helper")
    }


def test_astro_processed_script_is_typescript_by_default():
    modules, _ = parsed({"Page.astro": "<script>function run(x: string) {}</script>"})
    assert modules[0].functions[0].properties["language"] == "typescript"


@pytest.mark.parametrize("suffix", [".vue", ".svelte", ".astro"])
def test_doctor_checks_framework_parser(suffix):
    from grag.readiness import _grammar_check

    assert "extraction + parse passed" in _grammar_check(suffix, False)


def test_shadowed_require_does_not_create_an_import():
    _, edges = parsed(
        {
            "lib.js": "export function helper() {}",
            "main.js": """function run(require) { require('./lib'); }
function another() { require('./lib'); const require = other; }
""",
        }
    )
    assert pairs(edges, "IMPORTS") == set()


def test_astro_template_comment_does_not_create_symbols():
    modules, _ = parsed(
        {
            "Page.astro": """{/* <script>function fake() {}</script> */}
<script>function real() {}</script>"""
        }
    )
    assert [
        (n.properties["name"], n.properties["line_start"]) for n in modules[0].functions
    ] == [("real", 2)]


def test_framework_crlf_offsets_with_multiple_inline_unicode_blocks():
    modules, edges = parsed(
        {
            "Page.svelte": "<h1>héllo</h1>\r\n<script module>function helper() {} function run() {helper();}</script>\r\n<script>function helper() {} function run() {helper();}</script>"
        }
    )
    assert pairs(edges) == {
        ("Page.svelte#module.run", "Page.svelte#module.helper"),
        ("Page.svelte#instance.run", "Page.svelte#instance.helper"),
    }
    assert [n.properties["line_start"] for n in modules[0].functions] == [2, 2, 3, 3]


@pytest.mark.parametrize("suffix", [".astro", ".svelte", ".vue"])
def test_quoted_template_script_example_is_not_a_definition(suffix):
    modules, _ = parsed(
        {
            "Page" + suffix: """{ "<script>function fake() {}</script>" }
<script>function real() {}</script>"""
        }
    )
    assert [n.properties["name"] for n in modules[0].functions] == ["real"]
