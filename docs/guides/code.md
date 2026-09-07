# Index code

Use the MCP `ingest_code` tool when a shared server is running. Direct CLI
`ingest-code` opens the database itself; stop its owner and disconnect auto-starting
clients before using that route. [Server ownership](../operations/server.md) explains
how multiple harnesses share one database.

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
| Class | INHERITS (Python) | Class |
| Function | CALLS (Python) | Function |

Nodes carry path, line range, signature and docstring — **structure only, no source bodies** — with ids like `Module:repo-<canonical-path-sha256>:src/a.py` and `Function:repo-<canonical-path-sha256>:src/a.py#Class.method`. The path-derived repo component prevents same-named checkouts from colliding. Re-ingesting is incremental: every file is parsed (cross-file `IMPORTS`/`CALLS` need the whole set) but only files whose content changed are rewritten, and pruning of removed files, symbols and generated edges is scoped to those files. Three recipes:

```cypher
// what imports module X?
MATCH (m:Module)-[:IMPORTS]->(x:Module) WHERE x.path = 'core.py' RETURN m.id
// what calls function Y?
MATCH (f:Function)-[:CALLS]->(y:Function) WHERE y.path = 'core.py' AND y.name = 'helper' RETURN f.id
// cross-repo imports (multiple paths ingested into one db)
MATCH (r1:Repo)-[:CONTAINS_REPO_MODULE]->(a:Module)-[:IMPORTS]->(b:Module)<-[:CONTAINS_REPO_MODULE]-(r2:Repo)
WHERE r1.id <> r2.id RETURN a.id, b.id
```

Python parses via stdlib `ast` in every install. Everything else parses via tree-sitter and needs `pip install "gragdb[code]"`: TypeScript/JavaScript (`.ts .tsx .js .jsx .mjs .cjs`, plus the `<script>` block of `.vue`), C#, Terraform, Go, and — through `tree-sitter-language-pack` — Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala and SQL (tables as `Class`, views/functions/procedures as `Function`). Without the extra those files raise a hint-carrying error. Every language yields Module/Class/Function nodes with signature, doc comment and line range; CALLS/INHERITS edges are Python-only for now; IMPORTS is best-effort (path/package-based) elsewhere. For the language-pack languages a construct the grammar does not know costs one symbol, not the file.

## Language coverage

| Language / file type | Structure | IMPORTS | CALLS / INHERITS |
|---|---|---|---|
| Python | Modules, classes, functions | Best-effort local resolution | Best-effort Python resolution |
| TypeScript / JavaScript / Vue script blocks | Modules, classes, functions | Best-effort path/package resolution | Not extracted |
| C#, Go | Language-specific types and functions | Best-effort; Go uses local package names | Not extracted |
| Terraform | Modules and module-call source/version | Local references where resolvable | Not extracted |
| Bash, Java, Kotlin, Rust, C, C++, Ruby, PHP, Swift, Lua, Scala, SQL | Supported constructs through the optional language pack | Best-effort where implemented | Not extracted |
| Svelte / Astro | Not supported; script blocks are not indexed | Not extracted | Not extracted |

An empty `CALLS` result in TypeScript does not mean the function has no callers.
Use source search for relationships the parser does not extract. Dynamic dispatch,
external packages and unsupported constructs can also leave edges absent.

## Choose the input scope deliberately

In 0.8.0, scanning uses a fixed directory/file skip list. It does **not** honor
`.gitignore` or `.gragignore`, and it does not stop at nested worktree/repository
boundaries. A checkout containing `.claude/worktrees/` can index those copies too.
Inspect your tree before `init --ingest` or `ingest-code .`; prefer explicit source
paths when nested worktrees or generated trees are present.

Each supplied directory currently becomes its own `Repo` root; a supplied file
uses its parent. Paths are relative to those roots, not necessarily the enclosing
Git root. Multiple path arguments do not yet provide a single-root scoped ingest.
[Freshness](freshness.md) verifies the saved scope, including these limitations.
