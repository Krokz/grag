# Index code

Grag 0.9.0 adds shared-owner CLI routing, source selection,
Python/JS/TS/Go navigation and Java/C# overload identities described below.

Init-configured MCP clients share the owning server. CLI ingestion uses an existing
registered server; without one, the CLI opens the local database. A direct stdio
MCP process owns its database. [Server ownership](../operations/server.md)
explains how multiple harnesses share one database.

Point `ingest_code` at a repo and structural questions become cheap Cypher instead of file-reading spelunking. Two entry points, same engine:

```bash
# CLI
grag --db knowledge.lbdb ingest-code src/ ../other-repo --max-file-kb 2048
```

```
# MCP — an agent indexes a repo on demand (background=true for large trees)
ingest_code(paths=["src/"], calls=true, max_file_kb=1024)
```

| From | Relationship | To |
|---|---|---|
| Repo | CONTAINS_REPO_MODULE | Module |
| Module | CONTAINS_MODULE_CLASS / CONTAINS_MODULE_FUNCTION | Class / Function |
| Class | CONTAINS_CLASS_FUNCTION | Function |
| Module | IMPORTS | Module |
| Class | INHERITS (Python; static JS/TS extends) | Class |
| Function | CALLS (Python; static JS/TS and Go bindings) | Function |
| Module | CONTAINS_MODULE_CONSTANT (Go) | Constant |
| Class | IMPLEMENTS_INTERFACE (supported Go method sets) | Class |
| Module | CONTAINS_MODULE_MODULECALL (Terraform) | TerraformModuleCall |

Nodes carry path, line range, signature and docstring — **structure only, no source bodies** — with ids like `Module:repo-<canonical-path-sha256>:src/a.py` and `Function:repo-<canonical-path-sha256>:src/a.py#Class.method`. Java/C# functions append an overload signature digest (see below). The path-derived repo component prevents same-named checkouts from colliding. Re-ingesting is incremental: every file is parsed for cross-file resolution, while content, parser or dependency-derived changes determine which files are rewritten. Pruning is scoped to those files and removed sources. Three recipes:

```cypher
// what imports module X?
MATCH (m:Module)-[:IMPORTS]->(x:Module) WHERE x.path = 'core.py' RETURN m.id
// what calls function Y?
MATCH (f:Function)-[:CALLS]->(y:Function) WHERE y.path = 'core.py' AND y.name = 'helper' RETURN f.id
// cross-repo imports (multiple paths ingested into one db)
MATCH (r1:Repo)-[:CONTAINS_REPO_MODULE]->(a:Module)-[:IMPORTS]->(b:Module)<-[:CONTAINS_REPO_MODULE]-(r2:Repo)
WHERE r1.id <> r2.id RETURN a.id, b.id
```

Python parses via stdlib `ast` in every install. Other supported languages use tree-sitter and need `pip install "gragdb[code]"`: TypeScript/JavaScript (`.ts .tsx .js .jsx .mjs .cjs .mts .cts`, plus supported scripts in `.vue .svelte .astro`), C#, Terraform, Go, and — through `tree-sitter-language-pack` — Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala and SQL (tables as `Class`, views/functions/procedures as `Function`). Without the extra those files raise a hint-carrying error. Parsers emit supported Module/Class/Function and language-specific nodes where applicable, with available signatures, doc comments and line ranges. Python, JS/TS and Go have the relationship coverage below; other IMPORTS resolution is best-effort (path/package-based). Unsupported constructs can be omitted; inspect coverage and warnings before drawing conclusions from missing symbols.

## Language coverage

| Language / file type | Structure | IMPORTS | CALLS / INHERITS |
|---|---|---|---|
| Python | Modules, classes, functions, including conditional/nested declarations | Exact indexed paths and explicit bindings | Conservative lexical/import calls and class bases; partial coverage |
| TypeScript / JavaScript / Vue / Svelte / Astro scripts | Modules, classes, functions | Unambiguous relative paths within the indexed root | Local and explicit ES import bindings; runtime class extends |
| Go | Named types, functions, methods and constants | Exact indexed local `go.mod` package paths | Package/typed-receiver calls; basic interface method sets via IMPLEMENTS_INTERFACE |
| C# | Language-specific types and functions | Best-effort namespaces | Not extracted |
| Terraform | Modules and module-call source/version | Local references where resolvable | Not extracted |
| Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala, SQL | Supported constructs through the optional language pack | Best-effort where implemented | Not extracted |

An empty `CALLS` result does not mean a function has no callers. The new JS/TS
pass follows named functions, arrow/function variables, imported aliases, default
named exports, namespace imports and explicit named re-exports. It respects local
shadowing, destructuring and assignments. It resolves unique relative files and
`index` files within the same root, including unambiguous `.js` → `.ts` source
substitution. It never matches a bare package name to an unrelated local symbol.

This is static source evidence, not proof a call executes. `this`/object dispatch,
constructors, callbacks without indexed Function nodes, callable aliases, CommonJS
symbol bindings, wildcard re-exports, framework exports, tsconfig/bundler aliases
and external packages remain unresolved. Interfaces and `implements` do not create
runtime `INHERITS` edges. `calls=false` disables call edges while retaining structure,
imports and supported class inheritance.

Every JS/TS or framework `Module` has a JSON `code_coverage` STRING: partial mode,
call setting, counts of analyzed/resolved/unresolved sites, up to five diagnostic
line examples, script count and omitted script types. Missing counters mean zero.
These counts describe this analysis, not every possible runtime/template call.
The ingest response also reminds agents that coverage is partial. Query it before
making an absence claim:

```cypher
MATCH (m:Module) WHERE m.path = 'src/main.ts' RETURN m.code_coverage
MATCH (f:Function)-[r:CALLS]->(g:Function)
WHERE g.name = 'helper'
RETURN f.path, f.line_start, g.path, g.line_start, r._source, r.sites
```

Resolved JS/TS edges carry `resolution="js-static-v1"` and a JSON `sites` STRING:
`lines` contains up to 32 unique `[start,end]` source line pairs and `total` gives
the full unique-site count. Repeated calls share one edge. Definition citations
and edge sites refer to the original files, including CRLF/Unicode framework files.
Dependency export changes refresh affected callers even when their files did not
change. Parser revisions also invalidate old indexes automatically.

Framework scripts are parsed as independent units: Vue normal/setup scripts,
Svelte module/instance scripts, and Astro TypeScript frontmatter/browser scripts.
Prefixes such as `instance.run` and `frontmatter.run` distinguish symbols. Existing
Vue normal-script IDs stay unchanged; setup symbols use `setup.`. This pass omits
cross-block binding flow and all template expressions, event wiring and compiler
transformations. For example, Svelte permits instance code to reference module
bindings, but that cross-block call is not resolved yet. Astro frontmatter and
browser scripts are different execution environments. See the official
[Svelte scopes](https://svelte.dev/docs/svelte/svelte-files),
[Vue script setup](https://vuejs.org/api/sfc-script-setup.html#usage-alongside-normal-script),
and [Astro scripts](https://docs.astro.build/en/guides/client-side-scripts/).

HTML comments and inert template/code examples are ignored. External `src` scripts,
unsupported languages/types and files with no supported scripts appear in coverage;
syntax errors produce ingestion warnings and leave freshness unverified. The parser
uses the existing optional JS/TS grammars and `doctor` checks all three containers.
No compiler, model or additional service is required.

## Choose the input scope deliberately

Scanning honors `.gitignore` and `.gragignore` in the enclosing checkout and its
subdirectories. Rules use Git syntax, including negation; `.gragignore` is applied
after `.gitignore` in each directory. They apply to tracked and untracked files.
Global Git excludes are not used. Generated/VCS directories, symlinks and nested
repositories/worktrees are skipped. To index a nested repository, supply it as its
own directory root. Ignore rules never authorize following symlinks.

A directory argument remains its own `Repo` root. Explicit files use their enclosing
checkout (or their parent in a plain folder). Use `--root` to group selected paths
under a chosen enclosing root without indexing its other files:

```bash
grag ingest-code --root . src/api.py tests/test_api.py
# Replace that root's registered scope; omitted generated code is reconciled.
grag ingest-code --root . --replace-scope src/api.py
# Remove the registered code scope, preserving authored references/memories.
grag ingest-code --root . --replace-scope
```

MCP accepts `root` and `replace_scope` on `ingest_code`; an empty `paths` list with
both fields removes the scope. Ordinary re-ingests retain earlier registered paths
under the same root. [Freshness](freshness.md) uses exactly the saved selection.
New exclusions, including the file-size threshold, reconcile generated nodes and
edges. Authored/unknown relationships remain; referenced obsolete code nodes are
marked `_source_state="obsolete"` and omitted from current retrieval. Review the
warnings and use `evidence="all"` or Cypher to inspect retained evidence.

## Go navigation

Go works with the existing `gragdb[code]` extra, with no Go toolchain, language
server or dependency download at ingestion time. Index an enclosing root that
contains `go.mod` and the packages you want to navigate. Modules represent files;
Go package resolution groups files by repository, directory and declared package.
An import links to the selected files of the matching package, including multi-file
packages, aliases and declared package names that differ from the directory name.
The nearest `go.mod` inside the indexed root supplies its path. That metadata is
bounded, refuses symlinks and participates in freshness, including file-only scopes.
External packages and ambiguous module paths remain unresolved. The old package
basename heuristic is no longer used; without a local manifest, same-package calls
still work, while import resolution remains unverified.

`CALLS` includes unambiguous package functions and methods on statically known
parameters, method receivers, typed locals and simple composite-literal locals.
Local shadowing is conservative across the whole function. Function variables,
closures without indexed identities, promoted methods, complex receiver expressions
and dynamic dispatch remain unresolved. Calls through a known interface target its
method declaration (`resolution="go_interface_method"`), preserving the distinction
between a declared API and an implementation. Other resolution values distinguish
package functions, imported functions and concrete static receivers. Edge `sites`
contains up to 32 unique line ranges, with the total unique-range count.

Go types retain the existing `Class` label with a `kind` property (for example
`struct`, `interface`, `named`, `alias`). `IMPLEMENTS_INTERFACE` compares basic,
nonempty declared method sets with matching parameter/result type identities and
variadic signatures. Its `method_set` is `pointer` or `value_and_pointer`; it does
not create class inheritance. Embedded/promoted methods, generic constraints,
alias expansion, anonymous/function/array types in method signatures and empty
interfaces are omitted from this comparison. This is conservative static evidence,
not a compiler/type-check result or proof that a call runs. See Go's
[method sets and interfaces](https://go.dev/ref/spec#Method_sets).

`Constant` nodes make exact package-level constants searchable and directly queryable:

```cypher
MATCH (c:Constant) WHERE c.name = 'MaxRetries'
RETURN c.expression, c.declared_type, c.path, c.line_start, c.line_end,
       c.inherited_expression, c.expression_line, c.iota_index

MATCH (f:Function)-[r:CALLS]->(target:Function)
WHERE target.name = 'Load' AND target.path = 'store/methods.go'
RETURN f.name, f.path, f.line_start, r.resolution, r.sites

MATCH (t:Class)-[r:IMPLEMENTS_INTERFACE]->(i:Class)
WHERE i.name = 'Reader'
RETURN t.name, t.path, r.method_set
```

Expressions are exact source text, **not evaluated values**. For grouped constants,
`inherited_expression` and `expression_line` identify an omitted expression copied
from the preceding specification; `iota_index` records that specification's ordinal.
Thus `1 << iota` remains `1 << iota` with its index, rather than an invented numeric
value. Blank names are omitted. Constants support source lifecycle pruning, normal
search/context/paging and Markdown backtick links through `MENTIONS_CONSTANT`.

Every Go module carries partial `code_coverage`: package/import path, calls setting,
resolved/unresolved counters, bounded diagnostic line examples and limits. The scan
is a union of selected files: build tags, OS-specific filename selection, `go.work`,
`replace` and vendor resolution are not evaluated. Duplicate declarations from
build variants remain ambiguous. Empty edge results do not establish absence.
Changes to manifests, target declarations or interface signatures refresh affected
relationships even when caller/type files are unchanged. Parser revision changes
also refresh older indexes. `calls=false` removes calls while retaining constants,
imports, containment and supported interface method-set relationships.

## Python bindings

Python uses the standard-library AST and tracks lexical scopes, parameters,
assignments, imports, `global`/`nonlocal`, class scopes and comprehensions.
`from .core import helper as renamed` retains the original imported name.
Function-local imports stay local. A call through a parameter named `helper`
does not become a call to a same-named module function. No globally unique-name
or dotted-suffix guesses are made. Explicit absolute imports can cross indexed
roots when the path is unambiguous; relative imports remain within their root.

`CALLS`/`INHERITS` target known lexical or imported declarations. Direct method
receiver calls identify the method's declaration in that class; they do not
predict subclass dispatch. Writes conservatively invalidate bindings across the
scope. Arbitrary decorators, lambda bodies, definition-expression calls
(defaults/annotations/decorators), dynamic expressions and runtime monkey patching
are outside the supported analysis. Wildcard imports and `exec`/`eval` prevent
resolution where bindings cannot be established. No code or dependency is executed.

Each Python `Module.code_coverage` is a JSON STRING with `status="partial"`,
`calls_enabled`, analyzed/resolved/unresolved counters, bounded line examples,
omissions and limits. Missing counters mean zero. These describe the supported
analysis, not all runtime calls. Edges carry `resolution="python_static_binding"`
and bounded source-line `sites`. Changes to imported bindings update callers and
their coverage even when the caller's source is unchanged. A parser revision
refreshes older generated edges; authored/unknown links are preserved.

## Java and C# overload identities

Function IDs use `<module>#<qualified-name>~<signature-digest>` for **all** Java/C#
functions, so adding or removing an overload does not rename the others.
`identity_signature` records the normalized declaration syntax: parameter types,
generic arity and relevant constructor/interface distinctions. The digest excludes
line numbers, formatting, comments, local parameter names, defaults and bodies.
The normal `signature` property retains the full declaration header, including
multiline parameter lists. Type aliases, generic substitution and compiler type
equivalence are not evaluated. Other languages retain their existing IDs.

On the first refreshed scan, old managed Java/C# Function records are retained
with `_source_state="obsolete"`, including records without graph links: they may
contain authored properties or history. Generated containment is rebuilt for the
new identities. Authored/unknown relationships remain attached to the old record;
grag cannot infer which overload the author intended after an earlier ID collision.
The ingest response explains this transition. Current search excludes the obsolete
records; Cypher and `evidence="all"` can inspect them.

To review replacements for an old ID, query current candidates, compare their
signatures and citations, then explicitly reconcile any authored references:

```cypher
MATCH (f:Function)
WHERE f.id STARTS WITH '<old-function-id>~' AND f._source_state = 'current'
RETURN f.id, f.signature, f.path, f.line_start, f.identity_signature
```

Legacy records are intentionally retained until reviewed; ingestion does not
silently delete or retarget them. New overloads use ordinary source-deletion
handling, preserving obsolete nodes when authored references still need them.
This improves structural identities, not Java/C# call resolution.
