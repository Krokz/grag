# Go navigation

[All language support](index.md) · [Index code](../../guides/code.md)

## Select packages and manifests


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

## Calls and receivers

`CALLS` includes unambiguous package functions and methods on statically known
parameters, method receivers, typed locals and simple composite-literal locals.
Local shadowing is conservative across the whole function. Function variables,
closures without indexed identities, promoted methods, complex receiver expressions
and dynamic dispatch remain unresolved. Calls through a known interface target its
method declaration (`resolution="go_interface_method"`), preserving the distinction
between a declared API and an implementation. Other resolution values distinguish
package functions, imported functions and concrete static receivers. Edge `sites`
contains up to 32 unique line ranges, with the total unique-range count.

## Types and interfaces

Go types retain the existing `Class` label with a `kind` property (for example
`struct`, `interface`, `named`, `alias`). `IMPLEMENTS_INTERFACE` compares basic,
nonempty declared method sets with matching parameter/result type identities and
variadic signatures. Its `method_set` is `pointer` or `value_and_pointer`; it does
not create class inheritance. Embedded/promoted methods, generic constraints,
alias expansion, anonymous/function/array types in method signatures and empty
interfaces are omitted from this comparison. This is conservative static evidence,
not a compiler/type-check result or proof that a call runs. See Go's
[method sets and interfaces](https://go.dev/ref/spec#Method_sets).

## Query constants and relationships

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

## Coverage and refresh behavior

Every Go module carries partial `code_coverage`: package/import path, calls setting,
resolved/unresolved counters, bounded diagnostic line examples and limits. The scan
is a union of selected files: build tags, OS-specific filename selection, `go.work`,
`replace` and vendor resolution are not evaluated. Duplicate declarations from
build variants remain ambiguous. Empty edge results do not establish absence.
Changes to manifests, target declarations or interface signatures refresh affected
relationships even when caller/type files are unchanged. Parser revision changes
also refresh older indexes. `calls=false` removes calls while retaining constants,
imports, containment and supported interface method-set relationships.
