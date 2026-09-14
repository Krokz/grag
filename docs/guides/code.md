# Index code

Turn selected source files into definitions, relationships and file/line citations.
{ .grag-lead }

## Index a source directory

Python support is included. For other supported languages, install `gragdb[code]`.
Choose the intended source directory before running an ingest.

=== "CLI"

    ```bash
    grag ingest-code src/
    ```

=== "MCP"

    ```python
    ingest_code(paths=["src/"], calls=true, max_file_kb=1024)
    ```

    For large trees, use `background=true` and poll the returned job.

Init-configured MCP clients and CLI ingestion use the existing shared owner.
Without a registered owner, CLI ingestion opens the local database. A direct
stdio MCP process owns its file; see [multiple-client setup](../operations/server.md).

!!! note "Structure and citations, not source bodies"
    Nodes carry paths, line ranges, signatures and docstrings. Read the cited
    source when you need implementation details. Static analysis is partial;
    an empty caller result is not proof that no callers exist.

## Ask structural questions

Check `describe_schema` before querying. These recipes use the standard code schema:

=== "Imports"

    ```cypher
    MATCH (m:Module)-[:IMPORTS]->(x:Module)
    WHERE x.path = 'core.py'
    RETURN m.id
    ```

=== "Callers"

    ```cypher
    MATCH (f:Function)-[:CALLS]->(y:Function)
    WHERE y.path = 'core.py' AND y.name = 'helper'
    RETURN f.id
    ```

=== "Across repositories"

    ```cypher
    MATCH (r1:Repo)-[:CONTAINS_REPO_MODULE]->(a:Module)
          -[:IMPORTS]->(b:Module)<-[:CONTAINS_REPO_MODULE]-(r2:Repo)
    WHERE r1.id <> r2.id
    RETURN a.id, b.id
    ```

## Language coverage

Python, JavaScript/TypeScript and Go have conservative static relationship
analysis. Other supported languages provide structures and best-effort imports
where implemented. Read the [coverage matrix](../reference/languages/index.md)
for your language before drawing conclusions from missing symbols or edges.

### Go navigation

[Packages, typed receivers, constants and interfaces →](../reference/languages/go.md)

### Python bindings

[Lexical scopes, imports, calls and their limits →](../reference/languages/python.md)

### Java and C# overload identities

[Stable identities and review of older records →](../reference/languages/overloads.md)

[JavaScript, TypeScript and framework scripts →](../reference/languages/javascript.md)

## Choose the input scope deliberately

Scans honor `.gitignore` and `.gragignore` and skip nested repositories/worktrees
and symlinks. Explicit file paths still honor ignores. Use `--root` for selected
files that should share an enclosing source root.

### Repositories that ignore everything by default

A leading `*` can exclude parent directories even when a file pattern is allowed.
See [directory exceptions and Terraform example](source-scope.md#repositories-that-ignore-everything-by-default).

### Explicit source roots

[Select, replace or remove an enrolled scope →](source-scope.md#explicit-source-roots)

## Incremental ingestion

New in 0.10.0: repeated ingests in the same database owner reuse unchanged
parse summaries. Files are still read and content-hashed, ignore and size policies
are reapplied, and relationships are resolved against the complete selected scope.
An unchanged caller can gain or lose a target when another file, export, namespace
or Go manifest changes. Dependency-only changes update module coverage and edges
without rewriting unchanged function/class declarations.


??? info "Cache limits, counters and publication semantics"

    The cache needs no setup. It holds at most 2,048 files and an estimated 32 MiB of
    Python summary objects per open database, retaining no syntax trees or source bodies.
    It is disposable: eviction and server restart mean parsing again, not losing graph
    memory. Cold ingestion, source verification and dependency resolution still do work;
    large changes can spend most of their time publishing graph updates. These limits
    bound retained cache entries, not total process memory or temporary parsing allocations.

    The response keeps `files_parsed` as the count of successfully processed files for
    compatibility. `files_reused` is the subset whose parse summaries came from cache;
    `files_parsed - files_reused` gives fresh parses. `files_unchanged` counts files whose
    graph writes were skipped. A reused parse can still require dependency updates.
    Python/REST `incremental=false` forces parsing and rewriting. Old stored fingerprints
    are rewritten once on the first ingest; identities and authored links are preserved.

    A cache hit is never a successful-publication or freshness receipt. Graph updates
    and fingerprints still commit atomically, and `freshness=require` still verifies
    the enrolled source. Separate harnesses sharing one server also share its warm cache;
    a new direct CLI/stdio owner starts cold.

## Code graph relationships

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



IDs include the canonical checkout path hash, for example
`Module:repo-<canonical-path-sha256>:src/a.py` and
`Function:repo-<canonical-path-sha256>:src/a.py#Class.method`. This keeps same-named
checkouts distinct. Java/C# functions also include an overload signature digest.
Pruning is scoped to changed and removed sources; authored references are preserved.

**Next:** [check source freshness](freshness.md) or [add documentation](documents.md).
