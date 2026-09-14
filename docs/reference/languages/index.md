# Language support

Find the structures and relationships available for your source language.
All relationship analysis is partial; missing edges do not establish absence.
{ .grag-lead }

## Coverage at a glance


| Language / file type | Structure | IMPORTS | CALLS / INHERITS |
|---|---|---|---|
| Python | Modules, classes, functions, including conditional/nested declarations | Exact indexed paths and explicit bindings | Conservative lexical/import calls and class bases; partial coverage |
| TypeScript / JavaScript / Vue / Svelte / Astro scripts | Modules, classes, functions | Unambiguous relative paths within the indexed root | Local and explicit ES import bindings; runtime class extends |
| Go | Named types, functions, methods and constants | Exact indexed local `go.mod` package paths | Package/typed-receiver calls; basic interface method sets via IMPLEMENTS_INTERFACE |
| C# | Language-specific types and functions | Best-effort namespaces | Not extracted |
| Terraform | Modules and module-call source/version | Local references where resolvable | Not extracted |
| Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala, SQL | Supported constructs through the optional language pack | Best-effort where implemented | Not extracted |



## Choose your language

<div class="grid cards" markdown>

-   **Python**

    Lexical bindings, imports, calls and class bases using the standard-library AST.

    [Python details →](python.md)

-   **JavaScript and TypeScript**

    Static ES bindings, class inheritance and supported Vue, Svelte and Astro scripts.

    [JS/TS and frameworks →](javascript.md)

-   **Go**

    Package navigation, known receivers, constants and basic interface method sets.

    [Go details →](go.md)

-   **Java and C#**

    Stable overload identities and review of records from older indexes.

    [Identity and migration rules →](overloads.md)

</div>

## Parser installation

Python parses via stdlib `ast` in every install. Other supported languages use tree-sitter and need `pip install "gragdb[code]"`: TypeScript/JavaScript (`.ts .tsx .js .jsx .mjs .cjs .mts .cts`, plus supported scripts in `.vue .svelte .astro`), C#, Terraform, Go, and — through `tree-sitter-language-pack` — Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala and SQL (tables as `Class`, views/functions/procedures as `Function`). Without the extra those files raise a hint-carrying error. Parsers emit supported Module/Class/Function and language-specific nodes where applicable, with available signatures, doc comments and line ranges. Python, JS/TS and Go have the relationship coverage below; other IMPORTS resolution is best-effort (path/package-based). Unsupported constructs can be omitted; inspect coverage and warnings before drawing conclusions from missing symbols.


[Start indexing →](../../guides/code.md)
