"""Long-tail languages via tree-sitter-language-pack + the spec-driven walker:
bash, java, kotlin, rust, c, cpp, ruby, php, swift, lua, scala, sql, vue."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_language_pack")

from grag.core.types import CodeIngestRequest
from grag.ingest.code import ingest_code

SAMPLES: dict[str, str] = {
    "tools/build.sh": '''#!/bin/bash
source ./lib/util.sh
# greet someone
greet() {
  echo "hi $1"
}
function build_all {
  greet x
}
''',
    "tools/lib/util.sh": "util_fn() { :; }\n",
    "com/acme/app/Greeter.java": '''package com.acme.app;
import com.acme.core.Engine;
import java.util.List;
/** Doc. */
public class Greeter extends Base implements Runnable {
  public Greeter(int x) {}
  /** greet */
  public String greet(String name) { return "hi"; }
  interface Inner { void run(); }
}
enum Color { RED }
record Point(int x, int y) {}
''',
    "com/acme/core/Engine.java": "package com.acme.core;\npublic class Engine { public void run() {} }\n",
    "kt/Greeter.kt": '''package com.acme.app
import com.acme.core.Engine
class Greeter(val x: Int) : Base() {
    fun greet(name: String): String = "hi"
    companion object { fun make() = Greeter(1) }
}
interface Shape { fun area(): Double }
fun topLevel(a: Int): Int = a
''',
    "rs/src/lib.rs": '''use crate::engine::Engine;
mod engine;
/// Doc
pub struct Greeter { x: i32 }
pub trait Shape { fn area(&self) -> f64; }
impl Greeter {
    /// greet
    pub fn greet(&self, name: &str) -> String { "hi".into() }
}
impl Shape for Greeter { fn area(&self) -> f64 { 1.0 } }
pub fn top_level(a: i32) -> i32 { a }
mod inner { pub fn nested() {} }
''',
    "rs/src/engine.rs": "pub struct Engine;\nimpl Engine { pub fn run(&self) {} }\n",
    "c/main.c": '''#include <stdio.h>
#include "util.h"
struct point { int x; };
/* doc */
static int helper(int a) { return a; }
int main(int argc, char **argv) { return helper(1); }
''',
    "c/util.h": "int util(void);\n",
    "cpp/greeter.cpp": '''#include "greeter.hpp"
namespace acme {
class Greeter : public Base {
public:
  std::string greet(const std::string& n);
  void inline_fn() { }
};
std::string Greeter::greet(const std::string& n) { return "hi"; }
template <typename T> T ident(T v) { return v; }
int free_fn() { return 1; }
}
''',
    "rb/greeter.rb": '''require 'json'
require_relative 'lib/util'
# Doc
class Greeter < Base
  def initialize(x); @x = x; end
  # greet
  def greet(name) = "hi"
  def self.make; new(1); end
end
module Helpers
  def helper; end
end
def top_level(a); a; end
''',
    "rb/lib/util.rb": "def util; end\n",
    "php/Greeter.php": '''<?php
namespace Acme\\App;
use Acme\\Core\\Engine;
require_once __DIR__ . '/util.php';
/** Doc */
class Greeter extends Base {
    public function __construct(private int $x) {}
    public function greet(string $name): string { return "hi"; }
}
interface Shape { public function area(): float; }
trait Loggable { public function log() {} }
function top_level(int $a): int { return $a; }
''',
    "php/util.php": "<?php\nfunction util() {}\n",
    "swift/Greeter.swift": '''import Foundation
class Greeter: Base {
    init(x: Int) {}
    func greet(_ name: String) -> String { return "hi" }
}
struct Point { var x: Int }
protocol Shape { func area() -> Double }
extension Greeter { func extra() {} }
func topLevel(a: Int) -> Int { return a }
''',
    "lua/greeter.lua": '''local util = require("lib.util")
local M = {}
-- doc
function M.greet(name) return "hi" end
function M:method(x) return x end
local function helper(a) return a end
function top_level(a) return a end
M.arrow = function(x) return x end
return M
''',
    "scala/Greeter.scala": '''package com.acme.app
import com.acme.core.Engine
class Greeter(x: Int) extends Base {
  def greet(name: String): String = "hi"
}
object Registry { def get(): Int = 1 }
trait Shape { def area(): Double }
def topLevel(a: Int): Int = a
''',
    "sql/schema.sql": '''CREATE TABLE users (id INT PRIMARY KEY, name TEXT);
CREATE VIEW active_users AS SELECT * FROM users WHERE id > 0;
CREATE FUNCTION add_one(a INT) RETURNS INT AS $$ SELECT a + 1 $$ LANGUAGE SQL;
CREATE PROCEDURE cleanup() BEGIN DELETE FROM users; END;
''',
    "vue/App.vue": '''<template><div>{{ x }}</div></template>
<script lang="ts">
import { helper } from "./helper";
export class Store { load() { return 1; } }
export function setup() { return helper(); }
</script>
<style>div {}</style>
''',
    "vue/helper.ts": "export function helper() { return 1; }\n",
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    root = tmp_path_factory.mktemp("poly")
    for rel, text in SAMPLES.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    from grag.config import GragConfig
    from grag.core.engine import Engine

    engine = Engine(GragConfig(db_path=root / "poly.lbdb", buffer_pool_size=128 * 1024 * 1024))
    resp = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    yield engine, resp, root
    engine.close()


def _names(engine, label: str, path_suffix: str) -> dict[str, dict]:
    rows = engine.execute(
        f"MATCH (n:{label}) WHERE n.path ENDS WITH $p RETURN n.name, n.is_method, n.docstring, n.id",
        {"p": path_suffix},
    ).rows if label == "Function" else engine.execute(
        f"MATCH (n:{label}) WHERE n.path ENDS WITH $p RETURN n.name, NULL, n.docstring, n.id",
        {"p": path_suffix},
    ).rows
    return {r[0]: {"is_method": r[1], "doc": r[2], "id": r[3]} for r in rows}


def _imports(engine, path_suffix: str) -> set[str]:
    return {
        r[0]
        for r in engine.execute(
            "MATCH (m:Module)-[:IMPORTS]->(x:Module) WHERE m.path ENDS WITH $p RETURN x.path",
            {"p": path_suffix},
        ).rows
    }


def test_all_samples_parse_without_being_skipped(graph):
    engine, resp, _root = graph
    skipped = [w for w in resp.warnings if "could not parse" in w or "no parser" in w]
    assert skipped == []
    langs = {r[0] for r in engine.execute("MATCH (m:Module) RETURN DISTINCT m.language").rows}
    assert {"bash", "java", "kotlin", "rust", "c", "cpp", "ruby", "php", "swift", "lua", "scala", "sql", "typescript"} <= langs


def test_bash(graph):
    engine, _, _ = graph
    fns = _names(engine, "Function", "build.sh")
    assert set(fns) == {"greet", "build_all"}
    assert fns["greet"]["doc"] == "greet someone"
    assert _imports(engine, "build.sh") == {"tools/lib/util.sh"}


def test_java(graph):
    engine, _, _ = graph
    classes = _names(engine, "Class", "Greeter.java")
    assert set(classes) == {"Greeter", "Inner", "Color", "Point"}
    assert classes["Greeter"]["doc"] == "Doc."
    fns = _names(engine, "Function", "Greeter.java")
    assert fns["greet"]["is_method"] is True and fns["greet"]["doc"] == "greet"
    assert fns["Greeter"]["is_method"] is True  # constructor
    assert _imports(engine, "app/Greeter.java") == {"com/acme/core/Engine.java"}


def test_kotlin(graph):
    engine, _, _ = graph
    assert {"Greeter", "Shape"} <= set(_names(engine, "Class", "Greeter.kt"))
    fns = _names(engine, "Function", "Greeter.kt")
    assert {"greet", "make", "topLevel"} <= set(fns)
    assert fns["greet"]["is_method"] is True and fns["topLevel"]["is_method"] is False


def test_rust(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "lib.rs")) == {"Greeter", "Shape"}
    fns = _names(engine, "Function", "lib.rs")
    assert {"greet", "area", "top_level", "nested"} <= set(fns)
    assert fns["greet"]["is_method"] is True and fns["greet"]["doc"] == "greet"
    assert fns["greet"]["id"].endswith("#Greeter.greet")
    assert fns["nested"]["id"].endswith("#inner.nested")
    # impl methods link to the same-file struct
    linked = engine.execute(
        "MATCH (c:Class)-[:CONTAINS_CLASS_FUNCTION]->(f:Function) WHERE c.name = 'Greeter' AND c.path ENDS WITH 'lib.rs' RETURN f.name"
    ).rows
    assert {r[0] for r in linked} == {"greet", "area"}
    assert _imports(engine, "lib.rs") == {"rs/src/engine.rs"}


def test_c_and_cpp(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "main.c")) == {"point"}
    fns = _names(engine, "Function", "main.c")
    assert set(fns) == {"helper", "main"} and fns["helper"]["doc"] == "doc"
    assert _imports(engine, "main.c") == {"c/util.h"}
    cpp_fns = _names(engine, "Function", "greeter.cpp")
    assert {"greet", "inline_fn", "ident", "free_fn"} <= set(cpp_fns)
    assert cpp_fns["greet"]["is_method"] is True
    assert cpp_fns["greet"]["id"].endswith("#acme.Greeter.greet") or cpp_fns["greet"]["id"].endswith("#Greeter.greet")
    assert cpp_fns["free_fn"]["id"].endswith("#acme.free_fn")


def test_ruby(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "greeter.rb")) == {"Greeter", "Helpers"}
    fns = _names(engine, "Function", "greeter.rb")
    assert {"initialize", "greet", "make", "helper", "top_level"} <= set(fns)
    assert fns["greet"]["is_method"] is True and fns["greet"]["doc"] == "greet"
    assert _imports(engine, "greeter.rb") == {"rb/lib/util.rb"}


def test_php(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "Greeter.php")) == {"Greeter", "Shape", "Loggable"}
    fns = _names(engine, "Function", "Greeter.php")
    assert fns["greet"]["is_method"] is True and fns["top_level"]["is_method"] is False
    assert _imports(engine, "Greeter.php") == {"php/util.php"}


def test_swift(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "Greeter.swift")) == {"Greeter", "Point", "Shape"}
    fns = _names(engine, "Function", "Greeter.swift")
    assert {"init", "greet", "area", "extra", "topLevel"} <= set(fns)
    assert fns["extra"]["id"].endswith("#Greeter.extra")  # extension method


def test_lua(graph):
    engine, _, _ = graph
    fns = _names(engine, "Function", "greeter.lua")
    assert {"greet", "method", "helper", "top_level", "arrow"} <= set(fns)
    assert fns["greet"]["id"].endswith("#M.greet") and fns["greet"]["doc"] == "doc"


def test_scala(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "Greeter.scala")) == {"Greeter", "Registry", "Shape"}
    fns = _names(engine, "Function", "Greeter.scala")
    assert {"greet", "get", "area", "topLevel"} <= set(fns)


def test_sql_tolerates_unknown_statements(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "schema.sql")) == {"users"}
    assert {"active_users", "add_one"} <= set(_names(engine, "Function", "schema.sql"))


def test_vue_script_block(graph):
    engine, _, _ = graph
    assert set(_names(engine, "Class", "App.vue")) == {"Store"}
    fns = _names(engine, "Function", "App.vue")
    assert {"load", "setup"} <= set(fns)
    rows = engine.execute(
        "MATCH (f:Function) WHERE f.path ENDS WITH 'App.vue' AND f.name = 'setup' RETURN f.line_start"
    ).rows
    assert rows == [[5]]  # line numbers are those of the .vue file
    assert _imports(engine, "App.vue") == {"vue/helper.ts"}
