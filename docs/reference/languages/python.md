# Python bindings

[All language support](index.md) · [Index code](../../guides/code.md)

## Names and imports


Python uses the standard-library AST and tracks lexical scopes, parameters,
assignments, imports, `global`/`nonlocal`, class scopes and comprehensions.
`from .core import helper as renamed` retains the original imported name.
Function-local imports stay local. A call through a parameter named `helper`
does not become a call to a same-named module function. No globally unique-name
or dotted-suffix guesses are made. Explicit absolute imports can cross indexed
roots when the path is unambiguous; relative imports remain within their root.

## Calls, class bases and limits

`CALLS`/`INHERITS` target known lexical or imported declarations. Direct method
receiver calls identify the method's declaration in that class; they do not
predict subclass dispatch. Writes conservatively invalidate bindings across the
scope. Arbitrary decorators, lambda bodies, definition-expression calls
(defaults/annotations/decorators), dynamic expressions and runtime monkey patching
are outside the supported analysis. Wildcard imports and `exec`/`eval` prevent
resolution where bindings cannot be established. No code or dependency is executed.

## Coverage and refresh behavior

Each Python `Module.code_coverage` is a JSON STRING with `status="partial"`,
`calls_enabled`, analyzed/resolved/unresolved counters, bounded line examples,
omissions and limits. Missing counters mean zero. These describe the supported
analysis, not all runtime calls. Edges carry `resolution="python_static_binding"`
and bounded source-line `sites`. Changes to imported bindings update callers and
their coverage even when the caller's source is unchanged. A parser revision
refreshes older generated edges; authored/unknown links are preserved.
