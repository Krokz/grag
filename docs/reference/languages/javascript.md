# JavaScript, TypeScript and frameworks

[All language support](index.md) · [Index code](../../guides/code.md)

!!! note "Static coverage"
    Missing `CALLS` edges do not mean a function has no callers. Read the module coverage and source before making an absence claim.

## Supported bindings

The JS/TS pass follows named functions, arrow/function variables, imported aliases, default
named exports, namespace imports and explicit named re-exports. It respects local
shadowing, destructuring and assignments. It resolves unique relative files and
`index` files within the same root, including unambiguous `.js` → `.ts` source
substitution. It never matches a bare package name to an unrelated local symbol.

## Unresolved constructs

This is static source evidence, not proof a call executes. `this`/object dispatch,
constructors, callbacks without indexed Function nodes, callable aliases, CommonJS
symbol bindings, wildcard re-exports, framework exports, tsconfig/bundler aliases
and external packages remain unresolved. Interfaces and `implements` do not create
runtime `INHERITS` edges. `calls=false` disables call edges while retaining structure,
imports and supported class inheritance.

## Inspect coverage and source sites

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

## Vue, Svelte and Astro

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
