<!-- grag-managed skill reference: ingestion -->
# Indexing source and documents

`ingest_code` stores structure, signatures, docstrings, provenance and line ranges,
not source bodies. Python uses stdlib AST. Other supported languages need the
`gragdb[code]` extra. `ingest_docs` handles server-local Markdown/text and JSON/JSONL
document records: JSON needs a list of `{text, source?, metadata?}` or a `documents`
wrapper; JSONL needs one record per line. grag 0.10.0 adds explicit
`json_mode="document"` (CLI `--json-mode document`) for ordinary .json schemas,
contracts and fixtures. It validates syntax and indexes literal text with file
citations and `meta` format/coverage/source_sha256; no schema validation, JSON
property graph, field-level line citations or `$ref` resolution/fetching. All
selected .json files use that mode, even record-shaped ones; split calls by intent.
The default remains records, and JSONL is always records. Unsupported shapes in
record mode are skipped with warnings. Document mode accepts UTF-8/BOM and at most
64 nesting levels; existing 256-file/document, 2 MiB loaded-byte and mutation
limits apply. Invalid files preserve unseen documents; resource limits abort.
Long chunks may be partial JSON, so inspect the cited file for full structure.
Read initial loader warnings as well as background job results. CSV is not supported;
Markdown sections preserve hierarchy and code mentions. Ingest code first when
those links are useful. Use `background=true` for large jobs; poll `job_status`
until done and inspect result warnings. Queued/running/job ID is not success.
Job records are process-local; cancelled or lost jobs may need resubmission.

## Scope and ownership

Selection honors `.gitignore` and `.gragignore`, skips symlinks and nested
repositories/worktrees, and reuses the same policy during code refresh. Directory
arguments are roots; files use the enclosing checkout. For code, use `root` to keep
selected paths within one intended scope. Registered paths accumulate by default;
`replace_scope=true` with an explicit root replaces them. Empty paths then unregister
that scope. `calls` and `max_file_kb` are saved for subsequent verification/refresh.

grag 0.10.0 reuses unchanged parse summaries within the owning process,
while reading source bytes and resolving current dependencies. `files_parsed` counts
processed files including the `files_reused` subset; `files_unchanged` counts skipped
graph writes. Reused callers can still need new relationships/coverage. Restart or
cache eviction causes parsing again; it does not erase graph memory or prove freshness.

Explicit file paths and `root` do not bypass ignore rules. Inspect the full policy,
including later negations and excluded parent directories. File exceptions alone
cannot reopen excluded parents. When the intended source needs an exception, scope
`.gragignore` to that source and its parents; verify selected modules and warnings
before claiming coverage. Broadly disabling ignores can include unrelated files.

Re-ingestion reconciles generated nodes/edges, including newly excluded and removed
source. Authored or unknown links can retain obsolete endpoints; inspect warnings,
`_source_state` and `_document_state`. Current retrieval excludes obsolete nodes.
Pre-ownership legacy links remain for explicit review. Do not assume they were
removed or certified by a new scan. Caller-upserted relationships remain authored.
Keep authored memory intact during scope changes; report stale prose separately
unless the user requested its correction.

Successful document-directory scans synchronize removed files and shrinking JSON
batches. Failed/incomplete scans preserve unseen documents and warn. Changes between
sections/flat mode or chunk labels reconcile prior generated structure. Documents
still require explicit re-ingestion after edits; code freshness does not verify them.

Code reads verify enrolled scopes in serving processes. For current code use
`freshness=require`; a timeout/error/unknown scope is not fresh. `GET /api/index/status`
shows saved options, root failures and retries. Plain folders work too. Legacy indexes
without saved options need explicit ingestion; never infer an entire directory from
an old file-only scope. A Python GragService must enable_auto_refresh() for verification.
Moved sources need explicit relocation; indexing should not create a replacement DB.

## Parser coverage

Supported optional grammars include JS/TS and Vue/Svelte/Astro scripts, C#, Terraform,
Go, Bash, Java, Kotlin, Rust, C/C++, Ruby, PHP, Swift, Lua, Scala and SQL. Missing
grammars fail with installation guidance.

Python CALLS/INHERITS use lexical and explicit import bindings, retaining aliases
and respecting parameter/local/global/nonlocal shadowing. Function-local imports
stay local; globally unique names are not evidence of binding. Module.code_coverage
reports partial analysis, unresolved sites and omitted constructs. Runtime dispatch,
arbitrary decorators, lambdas and dynamic rebinding are not inferred. Dependency
and parser changes refresh generated edges while preserving authored/unknown links.

Java/C# Function IDs append a normalized parameter-signature digest. Adding/removing
overloads or shifting lines does not rename remaining declarations. identity_signature
exposes the syntax basis; signature preserves the full header. Compiler type
equivalence is not evaluated. Older managed IDs are retained as obsolete records,
including authored properties/history/links; references are not guessed or retargeted.
Find current candidates with Function.id STARTS WITH '<old-id>~', inspect signatures,
then explicitly reconcile authored references. Current retrieval excludes obsolete
records; Cypher/evidence=all can inspect them. Other languages keep existing IDs.

JS/TS supports conservative lexical calls, explicit ES bindings, unambiguous relative
imports within the indexed root, named re-exports and runtime class extends.
`Module.code_coverage` is a JSON STRING with partial coverage, unresolved counts and
line examples. Edges have bounded `sites` and `resolution`. Framework script blocks
retain original line offsets and separate identities. Dynamic/receiver calls,
unindexed callbacks, template/cross-block calls, framework/CommonJS/wildcard exports,
bundler aliases and type-only inheritance remain unresolved. Empty edges never prove
absence. Other language imports/calls are also best-effort.

Go resolves same-package functions and known receiver/parameter/typed-local methods.
Imports need unambiguous local go.mod paths inside the indexed root; aliases and
multi-file packages work. No basename guesses or external dependency downloads.
Module.code_coverage records partial counts/examples/limits. Interface CALLS target
the interface declaration, not a concrete implementation. IMPLEMENTS_INTERFACE links
basic matching method sets with method_set=pointer or value_and_pointer; it is not
class inheritance or compiler validation. Embedded/promoted/generic/alias sets,
build tags, go.work/replace/vendor mapping, function variables and closures remain
unsupported. Duplicate build-variant declarations are ambiguous.

Constant nodes expose Go package constant name/expression/path/line_start/line_end.
Expressions are unevaluated source text; inherited_expression, expression_line and
iota_index explain grouped constants. Query Constant for exact constant/line questions
instead of expecting vectors to supply absent source values. Constants support normal
search/context and Markdown mentions. Manifest/dependency changes trigger code refresh.

Typical queries after schema inspection:

```cypher
MATCH (m:Module)-[:IMPORTS]->(x:Module) WHERE x.path = 'core.py'
RETURN m.id, m.path

MATCH (f:Function)-[:CALLS]->(g:Function) WHERE g.name = 'refresh'
RETURN f.id, f.path, f.line_start
```

Use narrow projected reads for exact locations; only fetch file bodies when needed.
Structural facts such as Terraform module source/version pins should come from
`TerraformModuleCall`, not manually copied prose that can drift.
