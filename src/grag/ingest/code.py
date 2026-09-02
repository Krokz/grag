"""Code ingestion: walk repo paths, parse structure, build a code graph.

`ingest_code` is the frozen library entry point (reached via
grag.service.GragService.ingest_code, the MCP tool, POST /api/ingest/code and
the `grag ingest-code` CLI). It records STRUCTURE ONLY — Repo/Module/Class/
Function nodes carrying path/line_start/line_end/signature/docstring/language,
plus TerraformModuleCall nodes (name/source/version) for `module` blocks in
.tf files, plus CONTAINS_*/IMPORTS/INHERITS/CALLS edges — so an LLM can answer
structural questions with cheap Cypher instead of retyping facts (like a
module's version pin) from memory or a doc that can drift from the source.
Source bodies stay out of the graph.

Ids are stable and human-readable: a Repo id combines its directory name with
a hash of its canonical path, a Module id is `<repo-id>:<relative/path>`, and a
Class/Function id is `<module_id>#<qualname>` (qualname dotted for nesting,
e.g. `ClassName.method`). Re-ingesting preserves unchanged nodes while pruning
definitions and generated edges no longer present in successfully parsed or
deleted source files. Every node/edge carries `_source` provenance.

Python parses via the stdlib `ast` module; TypeScript/JavaScript
(.ts/.tsx/.js/.jsx/.mjs/.cjs), C# (.cs), Terraform (.tf) and Go (.go) parse
via tree-sitter grammars in `grag.ingest.code_ts` (the `code` extra). `_PARSERS`
maps file suffix to parser so both share the walk/upsert pipeline. The
tree-sitter import is lazy: base installs run .py parsing fine, and parsing
a tree-sitter suffix without the extra raises ConfigurationError with an
install hint. Known code suffixes without a parser collect a non-fatal
warning.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import logging
import os
import posixpath
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    CodeIngestRequest,
    CodeIngestResponse,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)

log = logging.getLogger(__name__)

_S = PropertySpec  # shorthand, mirrors examples/build_self.py

# CONTAINS is split into one rel table per (FROM, TO) pair. Verified
# empirically: LadybugDB itself supports multi-pair rel tables via raw DDL,
# but grag's write path does not — define_schema rejects duplicate table names
# in one request, a same-named rel in a later define_schema call is silently
# skipped (if_not_exists), and upsert_edges validates every edge against the
# rel's single registered (from, to) pair. Same workaround as
# examples/build_self.py's MENTIONS_MODULE / MENTIONS_CONCEPT.
_CODE_NODE_TABLES = [
    NodeTableSpec(
        name="Repo",
        primary_key="id",
        searchable=True,
        # git_commit/git_branch/ingested_at power staleness reporting (grag
        # doctor): "the index is N commits behind HEAD" instead of silently
        # answering from a rotted graph.
        properties=[
            _S(name="name"),
            _S(name="path"),
            _S(name="git_commit"),
            _S(name="git_branch"),
            _S(name="ingested_at"),
        ],
    ),
    NodeTableSpec(
        name="Module",
        primary_key="id",
        searchable=True,
        properties=[_S(name="path"), _S(name="language"), _S(name="name")],
    ),
    NodeTableSpec(
        name="Class",
        primary_key="id",
        searchable=True,
        properties=[
            _S(name="name"),
            _S(name="path"),
            _S(name="line_start", type="INT64"),
            _S(name="line_end", type="INT64"),
            _S(name="signature"),
            _S(name="docstring"),
            _S(name="language"),
        ],
    ),
    NodeTableSpec(
        name="Function",
        primary_key="id",
        searchable=True,
        properties=[
            _S(name="name"),
            _S(name="path"),
            _S(name="line_start", type="INT64"),
            _S(name="line_end", type="INT64"),
            _S(name="signature"),
            _S(name="docstring"),
            _S(name="language"),
            _S(name="is_method", type="BOOL"),
        ],
    ),
    # One per Terraform `module { ... }` block, local or remote source alike.
    # name/source/version are read straight off the block by the HCL parser
    # — never hand-typed — so a version pin here can't drift from the .tf
    # source the way a doc-copied fact can.
    NodeTableSpec(
        name="TerraformModuleCall",
        primary_key="id",
        searchable=True,
        properties=[
            _S(name="name"),
            _S(name="source"),
            _S(name="version"),
            _S(name="path"),
            _S(name="line_start", type="INT64"),
            _S(name="line_end", type="INT64"),
        ],
    ),
]

_CODE_REL_TABLES = [
    RelTableSpec(name="CONTAINS_REPO_MODULE", from_label="Repo", to_label="Module"),
    RelTableSpec(name="CONTAINS_MODULE_CLASS", from_label="Module", to_label="Class"),
    RelTableSpec(
        name="CONTAINS_MODULE_FUNCTION", from_label="Module", to_label="Function"
    ),
    RelTableSpec(
        name="CONTAINS_CLASS_FUNCTION", from_label="Class", to_label="Function"
    ),
    RelTableSpec(
        name="CONTAINS_MODULE_MODULECALL",
        from_label="Module",
        to_label="TerraformModuleCall",
    ),
    RelTableSpec(name="IMPORTS", from_label="Module", to_label="Module"),
    RelTableSpec(name="INHERITS", from_label="Class", to_label="Class"),
    RelTableSpec(name="CALLS", from_label="Function", to_label="Function"),
]

# (from_label, to_label) per CONTAINS_* rel, keyed by name since Class-vs-
# Function/Module-vs-TerraformModuleCall can't be inferred from the rel name
# alone once there are more than two CONTAINS_MODULE_* kinds.
_CONTAINS_ENDPOINTS = {
    "CONTAINS_MODULE_CLASS": ("Module", "Class"),
    "CONTAINS_MODULE_FUNCTION": ("Module", "Function"),
    "CONTAINS_CLASS_FUNCTION": ("Class", "Function"),
    "CONTAINS_MODULE_MODULECALL": ("Module", "TerraformModuleCall"),
}

_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        ".idea",
        ".vscode",
    }
)
_SKIP_FILE_PATTERNS = ("*.min.js",)

# Content-hashed bundle filenames emitted by Vite/webpack/rollup, e.g.
# `index-C2RYf8Fw.js`, `app-B3KT9Q2P.css`: a base name, a dash, then an 8+ char
# alphanumeric hash. These are build artifacts, not source — indexing them floods
# the graph with thousands of minified symbols. (fnmatch can't express "exactly 8
# of a class", so this is a regex matcher, applied alongside the globs.)
_HASHED_BUNDLE_RE = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_-]{8,}\.(js|mjs|css|map)$")


def _skip_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _SKIP_FILE_PATTERNS) or bool(
        _HASHED_BUNDLE_RE.match(name)
    )


def _repo_id(root: Path) -> str:
    """Stable repo key that cannot collide with a same-named directory."""

    canonical = root.expanduser().resolve()
    name = canonical.name or "repo"
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return f"{name}-{digest}"


# Known code suffixes WITHOUT a registered parser: walked files with these
# extensions collect one grouped warning instead of being silently ignored
# (truly unknown extensions — docs, images, lockfiles — are skipped as
# non-code). Parsed suffixes (.py + the tree-sitter set) are not listed here.
_UNSUPPORTED_CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".java",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".php",
        ".pl",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".swift",
        ".vue",
    }
)


# --- walk -----------------------------------------------------------------------


def _walk(
    paths: list[Path], max_file_kb: int, warnings: list[str]
) -> Iterator[tuple[Path, Path]]:
    """Yield (repo_root, file) for every ingestable file under `paths`.

    Recursive; each path may be a directory (walked, repo root) or a single
    file (its parent is the repo root). Skip directories, skip-file glob
    patterns and oversized files are never yielded; oversized files collect a
    warning. Traversal order is sorted, so ingestion is deterministic.
    """
    limit = max_file_kb * 1024
    for path in paths:
        if path.is_file():
            candidates = [(path.parent, path)]
        elif path.is_dir():
            candidates = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                candidates.extend(
                    (path, Path(dirpath) / name) for name in sorted(filenames)
                )
        else:
            warnings.append(f"skipped {path}: no such file or directory")
            continue
        for root, file in candidates:
            if _skip_file(file.name):
                continue
            try:
                size = file.stat().st_size
            except OSError as exc:
                warnings.append(f"skipped {file}: could not stat ({exc})")
                continue
            if size > limit:
                warnings.append(
                    f"skipped {file}: {size // 1024} KB exceeds max_file_kb={max_file_kb}"
                )
                continue
            yield root, file


# --- python parsing ---------------------------------------------------------------


@dataclass
class _ParsedModule:
    """One parsed module: its node, its contained defs, and the raw facts the
    cross-module resolution pass (IMPORTS/INHERITS/CALLS) needs."""

    module: UpsertNode
    dotted: str  # repo-relative dotted module path, e.g. "grag.ingest.code"
    classes: list[UpsertNode] = field(default_factory=list)
    functions: list[UpsertNode] = field(default_factory=list)
    # Terraform `module` blocks (name/source/version read off the block
    # itself, one per block); empty for every non-HCL parser.
    terraform_module_calls: list[UpsertNode] = field(default_factory=list)
    # Go methods whose receiver type may be declared in a DIFFERENT file of
    # the same package (idiomatic: a type's methods commonly spread across
    # files, e.g. cache/client.go declares Cache, cache/load.go adds a
    # method to it) — (receiver type name, function id), resolved against a
    # package-wide class index in code.ingest_code rather than linked
    # same-file like every other language's CONTAINS_CLASS_FUNCTION; empty
    # for every non-Go parser.
    go_method_links: list[tuple[str, str]] = field(default_factory=list)
    contains: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (rel, from_key, to_key)
    import_refs: list[str] = field(default_factory=list)  # dotted names to resolve
    # local name -> dotted base module, for `from X import name` (used to
    # resolve bare `name(...)` calls to imported functions, e.g. lazy imports).
    from_imports: dict[str, str] = field(default_factory=dict)
    class_bases: list[tuple[str, str]] = field(
        default_factory=list
    )  # (class qualname, base expr)
    class_ids: dict[str, str] = field(default_factory=dict)  # simple name -> class id
    func_ids: dict[str, str] = field(
        default_factory=dict
    )  # simple name -> module-level function id
    method_ids: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # class qual -> {name -> id}
    # (caller qualname, "name" | "self_attr", called name)
    call_refs: list[tuple[str, str, str]] = field(default_factory=list)
    # Extra module-index keys for non-Python import resolution (C# namespace
    # names, Terraform module dirs); empty for Python.
    aliases: list[str] = field(default_factory=list)


def _signature(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct the `class ...:` / `def ...:` first line (no body)."""
    if isinstance(node, ast.ClassDef):
        parts = [ast.unparse(b) for b in node.bases]
        parts.extend(ast.unparse(k) for k in node.keywords)
        return (
            f"class {node.name}({', '.join(parts)}):"
            if parts
            else f"class {node.name}:"
        )
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    sig = f"{prefix} {node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig + ":"


def _collect_calls(
    func: ast.FunctionDef | ast.AsyncFunctionDef, out: list[tuple[str, str]]
) -> None:
    """Collect (kind, name) call refs from one function's own body.

    kind "name" is a bare `foo(...)` call; kind "self_attr" is `self.foo(...)`.
    Nested defs/classes have their own call scope and are not descended into;
    other attribute calls (obj.m(), mod.f()) are unresolved in Wave A.
    """

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                target = child.func
                if isinstance(target, ast.Name):
                    out.append(("name", target.id))
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    out.append(("self_attr", target.attr))
            visit(child)

    visit(func)


def _dotted(rel_path: str, repo: str) -> str:
    """Repo-relative dotted module path: 'a/b.py' -> 'a.b', 'a/__init__.py' -> 'a'."""
    parts = rel_path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else repo


def _parse_python(
    path: Path,
    source: str,
    *,
    repo: str,
    rel_path: str,
    calls: bool,
) -> _ParsedModule:
    """Parse one Python file with stdlib ast into a _ParsedModule.

    Raises SyntaxError/ValueError on unparseable source; the caller collects
    it as a warning and skips the file.
    """
    module_id = f"{repo}:{rel_path}"
    dotted = _dotted(rel_path, repo)
    parsed = _ParsedModule(
        module=UpsertNode(
            label="Module",
            key=module_id,
            properties={"path": rel_path, "language": "python", "name": dotted},
            source=str(path),
        ),
        dotted=dotted,
    )
    tree = ast.parse(source)
    src = str(path)

    def add_class(node: ast.ClassDef, qual: str) -> None:
        cid = f"{module_id}#{qual}"
        parsed.classes.append(
            UpsertNode(
                label="Class",
                key=cid,
                properties={
                    "name": node.name,
                    "path": rel_path,
                    "line_start": node.lineno,
                    "line_end": node.end_lineno,
                    "signature": _signature(node),
                    "docstring": ast.get_docstring(node) or "",
                    "language": "python",
                },
                source=src,
            )
        )
        # No Class->Class containment rel exists (see _CODE_REL_TABLES), so
        # nested classes attach to the Module like top-level ones.
        parsed.contains.append(("CONTAINS_MODULE_CLASS", module_id, cid))
        parsed.class_ids.setdefault(node.name, cid)
        parsed.class_bases.extend((qual, ast.unparse(b)) for b in node.bases)

    def add_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        qual: str,
        parent_class: str | None,
    ) -> None:
        fid = f"{module_id}#{qual}"
        is_method = parent_class is not None
        parsed.functions.append(
            UpsertNode(
                label="Function",
                key=fid,
                properties={
                    "name": node.name,
                    "path": rel_path,
                    "line_start": node.lineno,
                    "line_end": node.end_lineno,
                    "signature": _signature(node),
                    "docstring": ast.get_docstring(node) or "",
                    "language": "python",
                    "is_method": is_method,
                },
                source=src,
            )
        )
        if parent_class is not None:
            parsed.contains.append(
                ("CONTAINS_CLASS_FUNCTION", f"{module_id}#{parent_class}", fid)
            )
            parsed.method_ids.setdefault(parent_class, {})[node.name] = fid
        else:
            # Nested functions (def inside def) attach to the Module: there is
            # no Function->Function containment rel.
            parsed.contains.append(("CONTAINS_MODULE_FUNCTION", module_id, fid))
            if "." not in qual:
                parsed.func_ids.setdefault(node.name, fid)
        if calls:
            refs: list[tuple[str, str]] = []
            _collect_calls(node, refs)
            parsed.call_refs.extend((qual, kind, name) for kind, name in refs)

    def visit(stmts: list[ast.stmt], prefix: str, parent_class: str | None) -> None:
        for node in stmts:
            if isinstance(node, ast.ClassDef):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                add_class(node, qual)
                visit(node.body, qual, qual)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                add_function(
                    node, qual, parent_class if prefix == parent_class else None
                )
                visit(node.body, qual, parent_class)

    visit(tree.body, "", None)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            parsed.import_refs.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: anchor at this module's package
                pkg = (
                    dotted.split(".")
                    if rel_path.endswith("__init__.py")
                    else dotted.split(".")[:-1]
                )
                keep = max(0, len(pkg) - (node.level - 1))
                base = ".".join(pkg[:keep] + (base.split(".") if base else []))
            if base:
                parsed.import_refs.append(base)
                # `from pkg import submodule`: the alias may name a module too.
                parsed.import_refs.extend(
                    f"{base}.{a.name}" for a in node.names if a.name != "*"
                )
                # `from pkg.mod import func` (incl. function-local lazy imports)
                # lets a bare `func(...)` call resolve to the imported function.
                for a in node.names:
                    if a.name != "*":
                        parsed.from_imports[a.asname or a.name] = base
    return parsed


def _tree_sitter_parser(suffix: str) -> Callable[..., _ParsedModule]:
    """Dispatch stub for the tree-sitter suffixes: imports code_ts (and thus
    tree-sitter) lazily on first parse, so base installs without the `code`
    extra still import this module and parse .py files fine. Without the
    extra, code_ts raises ConfigurationError with the install hint."""

    def parse(
        path: Path, source: str, *, repo: str, rel_path: str, calls: bool
    ) -> _ParsedModule:
        from grag.ingest import code_ts

        return code_ts.parse_file(suffix, path, source, repo=repo, rel_path=rel_path)

    return parse


# Suffix -> parser dispatch. Python uses stdlib ast; ts/js/cs/tf/go use the
# tree-sitter wrappers above (Wave B) — neither touches the walk or upsert
# pipeline.
_PARSERS: dict[str, Callable[..., _ParsedModule]] = {".py": _parse_python}
for _suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".tf", ".go"):
    _PARSERS[_suffix] = _tree_sitter_parser(_suffix)
del _suffix


# --- resolution (IMPORTS / INHERITS / CALLS over the whole scanned set) ------------


def _resolve_module_id(dotted: str, index: dict[str, list[str]]) -> str | None:
    """Exact match, else unique suffix match ('grag.cli' -> 'src.grag.cli');
    ambiguous or unknown resolves to None (external imports skip silently)."""
    exact = index.get(dotted, [])
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    matches = sorted(
        {mid for d, mids in index.items() if d.endswith(f".{dotted}") for mid in mids}
    )
    return matches[0] if len(matches) == 1 else None


def _resolve_imported_func(
    name: str,
    base: str,
    module_index: dict[str, list[str]],
    by_module: dict[str, _ParsedModule],
) -> str | None:
    """Resolve a bare call to a `from base import name` imported function.

    `base` is the imported module's dotted path; the function lives in that
    module's func_ids under `name`. Returns the function id, or None if the
    module isn't in the scanned set or doesn't define it (external import).
    """
    mid = _resolve_module_id(base, module_index)
    if mid and mid in by_module:
        return by_module[mid].func_ids.get(name)
    return None


def _resolve_base(
    expr: str,
    parsed: _ParsedModule,
    module_index: dict[str, list[str]],
    class_index: dict[str, list[str]],
    by_module: dict[str, _ParsedModule],
) -> str | None:
    """Resolve an unparsed base expression ('Foo', 'mod.Foo', 'Generic[T]') to
    a scanned Class id: same module first, then the named module, then a
    globally unique class of that name. Best-effort; None skips the edge."""
    name = expr.split("[")[0].strip()  # drop generics subscript
    if not name:
        return None
    parts = name.split(".")
    cls_name = parts[-1]
    if len(parts) == 1:
        own = parsed.class_ids.get(cls_name)
        if own:
            return own
    else:
        mid = _resolve_module_id(".".join(parts[:-1]), module_index)
        if mid and mid in by_module:
            found = by_module[mid].class_ids.get(cls_name)
            if found:
                return found
    matches = class_index.get(cls_name, [])
    return matches[0] if len(matches) == 1 else None


def _missing_module_sources(engine: Engine, directory_roots: set[Path]) -> set[str]:
    """Previously ingested files that disappeared below an authoritative root."""

    missing: set[str] = set()
    for root in directory_roots:
        prefix = f"{root}{os.sep}"
        rows = engine.execute(
            "MATCH (n:Module) WHERE n._source STARTS WITH $prefix "
            "RETURN DISTINCT n._source",
            {"prefix": prefix},
        ).rows
        for row in rows:
            source = row[0]
            if source and not Path(str(source)).exists():
                missing.add(str(source))
    return missing


def _prune_code_nodes(
    engine: Engine,
    *,
    authoritative_sources: set[str],
    desired_by_label_source: dict[str, dict[str, set[str]]],
) -> int:
    if not authoritative_sources:
        return 0
    pruned = 0
    for label in ("Module", "Class", "Function", "TerraformModuleCall"):
        desired_by_source = desired_by_label_source[label]
        for source in sorted(authoritative_sources):
            result = engine.execute_write(
                f"MATCH (n:{label}) WHERE n._source = $source "
                "AND NOT n.id IN $keys DETACH DELETE n RETURN count(n)",
                {
                    "source": source,
                    "keys": sorted(desired_by_source.get(source, set())),
                },
            )
            if result.rows:
                pruned += int(result.rows[0][0])
    return pruned


def _prune_code_edges(
    engine: Engine,
    *,
    authoritative_sources: set[str],
    desired: set[tuple[str, str, str, str]],
) -> int:
    """Delete only generated edges absent from the newly resolved graph."""

    if not authoritative_sources:
        return 0
    pruned = 0
    for spec in _CODE_REL_TABLES:
        rows = engine.execute(
            f"MATCH (a)-[r:{spec.name}]->(b) RETURN a.id, b.id, r._source"
        ).rows
        for from_key, to_key, source in rows:
            edge_key = (spec.name, str(from_key), str(to_key), str(source))
            if source not in authoritative_sources or edge_key in desired:
                continue
            result = engine.execute_write(
                f"MATCH (a)-[r:{spec.name}]->(b) "
                "WHERE a.id = $from_key AND b.id = $to_key "
                "AND r._source = $source DELETE r RETURN count(r)",
                {
                    "from_key": from_key,
                    "to_key": to_key,
                    "source": source,
                },
            )
            if result.rows:
                pruned += int(result.rows[0][0])
    return pruned


def _prune_legacy_repos(engine: Engine, repos: dict[str, UpsertNode]) -> int:
    pruned = 0
    for repo_id, repo in repos.items():
        repo_root = Path(str(repo.properties["path"])).resolve()
        legacy_prefix = f"{repo.properties['name']}:"
        for label in ("Module", "Class", "Function"):
            rows = engine.execute(
                f"MATCH (n:{label}) WHERE n.id STARTS WITH $prefix "
                "RETURN n.id, n._source",
                {"prefix": legacy_prefix},
            ).rows
            for node_id, source in rows:
                if not source:
                    continue
                try:
                    Path(str(source)).expanduser().resolve().relative_to(repo_root)
                except (OSError, ValueError):
                    continue
                result = engine.execute_write(
                    f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n RETURN count(n)",
                    {"id": node_id},
                )
                if result.rows:
                    pruned += int(result.rows[0][0])
        result = engine.execute_write(
            "MATCH (r:Repo) WHERE r.path = $path AND r.id <> $id "
            "DETACH DELETE r RETURN count(r)",
            {"path": repo.properties["path"], "id": repo_id},
        )
        if result.rows:
            pruned += int(result.rows[0][0])
    return pruned


# --- repo staleness metadata ------------------------------------------------------

_REPO_STALENESS_COLUMNS = ("git_commit", "git_branch", "ingested_at")

# Per-module fingerprint of (file bytes, parse options, parser revision),
# written outside the upsert path because reserved "_" props are grag-internal
# and — unlike a declared STRING prop — never enter the FTS index or the
# embedding text. A matching fingerprint on re-ingest means the file's nodes
# and generated edges are already correct and its writes are skipped.
_INGEST_HASH_PROP = "_ingest_hash"


def _ensure_repo_staleness_columns(engine: Engine) -> None:
    """ALTER pre-existing Repo/Module tables to add columns added after v0.
    define_schema never alters an existing table (if_not_exists skips it), so
    databases ingested before these columns existed need an explicit ADD.
    """
    _ensure_columns(engine, "Repo", _REPO_STALENESS_COLUMNS)
    _ensure_columns(engine, "Module", (_INGEST_HASH_PROP,))


def _ensure_columns(engine: Engine, table: str, columns: tuple[str, ...]) -> None:
    try:
        res = engine.execute(f"CALL TABLE_INFO('{table}') RETURN *")
    except GragError:
        return
    existing = {str(row[1]) for row in res.rows}
    for column in columns:
        if column not in existing:
            with suppress(GragError):
                engine.execute_write(f"ALTER TABLE {table} ADD {column} STRING")


def _ingest_hash(data: bytes, *, calls: bool) -> str:
    """Fingerprint of one source file under the current parse options.

    The grag version is folded in so a parser improvement re-ingests every
    file once after an upgrade instead of leaving stale structure behind.
    """
    from grag import __version__

    h = hashlib.sha256(data)
    h.update(f"\x00calls={int(calls)}\x00grag={__version__}".encode())
    return h.hexdigest()


def _stored_ingest_hashes(engine: Engine, roots: set[Path]) -> dict[str, str]:
    """_source -> recorded fingerprint for every Module below `roots`."""
    stored: dict[str, str] = {}
    for root in roots:
        prefix = f"{root}{os.sep}"
        try:
            rows = engine.execute(
                f"MATCH (n:Module) WHERE n._source STARTS WITH $prefix "
                f"AND n.{_INGEST_HASH_PROP} IS NOT NULL "
                f"RETURN n._source, n.{_INGEST_HASH_PROP}",
                {"prefix": prefix},
            ).rows
        except GragError:
            return {}
        for source, digest in rows:
            if source and digest:
                stored[str(source)] = str(digest)
    return stored


def _record_ingest_hashes(engine: Engine, hashes: dict[str, str]) -> None:
    """Stamp the fingerprint on each (just upserted) Module by its _source."""
    for source, digest in hashes.items():
        engine.execute_write(
            f"MATCH (n:Module) WHERE n._source = $source SET n.{_INGEST_HASH_PROP} = $h",
            {"source": source, "h": digest},
        )


def _git_state(root: Path) -> dict[str, str]:
    """{'git_commit', 'git_branch'} for a checkout; {} outside git / no git."""
    out: dict[str, str] = {}
    for prop, args in (
        ("git_commit", ["rev-parse", "HEAD"]),
        ("git_branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
    ):
        try:
            proc = subprocess.run(  # noqa: S603 — fixed git argv over a local path
                ["git", "-C", str(root), *args],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if proc.returncode != 0:
            return out
        out[prop] = proc.stdout.strip()
    return out


# --- public: ingest_code ----------------------------------------------------------


def ingest_code(
    engine: Engine, config: GragConfig, req: CodeIngestRequest
) -> CodeIngestResponse:
    """Walk `req.paths`, parse code structure, and upsert the code graph.

    The schema is defined idempotently on every call; all nodes go in via one
    batched upsert_nodes per label and all edges via one upsert_edges per rel
    type. Unreadable/unparseable/oversized files and unsupported code
    extensions are collected as warnings instead of failing the batch.
    """
    define_schema(
        engine,
        config,
        DefineSchemaRequest(
            node_tables=list(_CODE_NODE_TABLES), rel_tables=list(_CODE_REL_TABLES)
        ),
    )
    _ensure_repo_staleness_columns(engine)

    warnings: list[str] = []
    input_paths = [Path(p) for p in req.paths]
    roots: dict[str, Path] = {}
    directory_roots: set[Path] = set()
    for path in input_paths:
        if path.is_dir():
            root = path.resolve()
            directory_roots.add(root)
        elif path.is_file():
            root = path.resolve().parent
        else:
            continue
        roots[_repo_id(root)] = root
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repos: dict[str, UpsertNode] = {
        repo_id: UpsertNode(
            label="Repo",
            key=repo_id,
            properties={
                "name": root.name or "repo",
                "path": str(root),
                "ingested_at": ingested_at,
                **_git_state(root),
            },
            source=str(root),
        )
        for repo_id, root in roots.items()
    }
    parsed_modules: list[_ParsedModule] = []
    successful_sources: set[str] = set()
    # Files whose fingerprint differs from the one recorded at their last
    # ingest (or every parsed file on a full run). Only these touch the write
    # lock; unchanged files are parsed for cross-file resolution only.
    changed_sources: set[str] = set()
    new_hashes: dict[str, str] = {}
    stored_hashes = (
        _stored_ingest_hashes(engine, set(roots.values())) if req.incremental else {}
    )
    unsupported: dict[str, int] = {}

    for walked_root, walked_file in _walk(input_paths, req.max_file_kb, warnings):
        rel_path = walked_file.relative_to(walked_root).as_posix()
        root = walked_root.resolve()
        # Keep the lexical path below the canonical root so provenance-based
        # directory pruning remains scoped even when a source file is a symlink.
        file = root / rel_path
        suffix = file.suffix.lower()
        parser = _PARSERS.get(suffix)
        if parser is None:
            if suffix in _UNSUPPORTED_CODE_SUFFIXES:
                unsupported[suffix] = unsupported.get(suffix, 0) + 1
            continue
        repo = _repo_id(root)
        repo_name = root.name or "repo"
        try:
            raw = file.read_bytes()
            source = raw.decode("utf-8")
            parsed = parser(file, source, repo=repo, rel_path=rel_path, calls=req.calls)
            if rel_path == "__init__.py":
                parsed.dotted = repo_name
            parsed_modules.append(parsed)
            key = str(file)
            successful_sources.add(key)
            digest = _ingest_hash(raw, calls=req.calls)
            if not req.incremental or stored_hashes.get(key) != digest:
                changed_sources.add(key)
                new_hashes[key] = digest
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"skipped {file}: could not read ({exc})")
        except (SyntaxError, ValueError) as exc:
            warnings.append(f"skipped {file}: could not parse ({exc})")

    for suffix in sorted(unsupported):
        warnings.append(
            f"skipped {unsupported[suffix]} file(s) with extension '{suffix}': "
            "no parser registered (supported: .py, .ts, .tsx, .js, .jsx, "
            ".mjs, .cjs, .cs, .tf, .go)"
        )

    # Cross-module resolution: IMPORTS, INHERITS and CALLS need the whole
    # scanned set, so edges are built after every module is parsed.
    module_index: dict[str, list[str]] = {}
    class_index: dict[str, list[str]] = {}
    by_module: dict[str, _ParsedModule] = {}
    # (package directory, type name) -> class id, for resolving Go method
    # receivers: a type's methods routinely live in a different file of the
    # same package than the type declaration itself (see _walk_go), so
    # class ids can't be derived from the method's own file/module.
    go_class_by_pkg: dict[tuple[str, str], str] = {}
    for pm in parsed_modules:
        mid = str(pm.module.key)
        by_module[mid] = pm
        pkg_dir = posixpath.dirname(str(pm.module.properties.get("path", "")))
        for cid in pm.class_ids.values():
            go_class_by_pkg.setdefault((pkg_dir, cid.rsplit("#", 1)[1]), cid)
        # Index both the repo-relative dotted path and the repo-qualified one:
        # when the repo dir IS the top package (repo "pkg" holding core.py),
        # absolute imports say "pkg.core" while the relative dotted is "core".
        repo = mid.split(":", 1)[0]
        repo_name = str(repos[repo].properties["name"])
        # Aliases are the non-Python resolution keys: C# namespaces and
        # Terraform module dirs (populated by code_ts; empty for Python).
        index_keys = {pm.dotted, f"{repo_name}.{pm.dotted}"}
        index_keys.update(pm.aliases)
        index_keys.update(f"{repo_name}.{alias}" for alias in pm.aliases)
        for key in index_keys:
            module_index.setdefault(key, [])
            if mid not in module_index[key]:
                module_index[key].append(mid)
        for cid in pm.class_ids.values():
            simple = cid.rsplit("#", 1)[1].rsplit(".", 1)[-1]
            class_index.setdefault(simple, [])
            if cid not in class_index[simple]:
                class_index[simple].append(cid)

    # Global function index (simple name -> ids) for resolving bare calls to
    # from-imported or otherwise-referenced functions defined in scanned modules.
    func_index: dict[str, list[str]] = {}
    for pm in parsed_modules:
        for n in pm.functions:
            simple = str(n.key).rsplit("#", 1)[1].rsplit(".", 1)[-1]
            func_index.setdefault(simple, [])
            if str(n.key) not in func_index[simple]:
                func_index[simple].append(str(n.key))

    edges: dict[tuple[str, str, str], UpsertEdge] = {}

    def add_edge(
        type_: str, from_label: str, from_key: str, to_label: str, to_key: str, src: str
    ) -> None:
        if from_key == to_key and from_label == to_label:
            return
        edges.setdefault(
            (type_, from_key, to_key),
            UpsertEdge(
                type=type_,
                from_label=from_label,
                from_key=from_key,
                to_label=to_label,
                to_key=to_key,
                source=src,
            ),
        )

    for pm in parsed_modules:
        mid = str(pm.module.key)
        src = str(pm.module.source)
        add_edge(
            "CONTAINS_REPO_MODULE", "Repo", mid.split(":", 1)[0], "Module", mid, src
        )
        for rel, from_key, to_key in pm.contains:
            from_label, to_label = _CONTAINS_ENDPOINTS[rel]
            add_edge(rel, from_label, from_key, to_label, to_key, src)
        if pm.go_method_links:
            pkg_dir = posixpath.dirname(str(pm.module.properties.get("path", "")))
            for recv_type, fid in pm.go_method_links:
                target = go_class_by_pkg.get((pkg_dir, recv_type))
                if target:
                    add_edge("CONTAINS_CLASS_FUNCTION", "Class", target, "Function", fid, src)
                # else: receiver type not declared anywhere in the scanned
                # package (e.g. a generic/embedded edge case tree-sitter
                # didn't resolve) — the Function node still exists, just
                # unlinked, same as any other best-effort miss here.
        for ref in pm.import_refs:
            target = _resolve_module_id(ref, module_index)
            if target and target != mid:
                add_edge("IMPORTS", "Module", mid, "Module", target, src)
        for qual, expr in pm.class_bases:
            target = _resolve_base(expr, pm, module_index, class_index, by_module)
            if target:
                add_edge("INHERITS", "Class", f"{mid}#{qual}", "Class", target, src)
        for qual, kind, name in pm.call_refs:
            caller = f"{mid}#{qual}"
            if kind == "name":
                target = pm.func_ids.get(name)
                if target is None and name in pm.from_imports:
                    # `from base import name`; `name(...)` -> the imported function
                    target = _resolve_imported_func(
                        name, pm.from_imports[name], module_index, by_module
                    )
                if target is None:
                    # Unique global match across the scanned set (guarded/lazy calls).
                    matches = func_index.get(name, [])
                    target = matches[0] if len(matches) == 1 else None
            else:  # self_attr: method on the caller's own class
                cls_qual = qual.rsplit(".", 1)[0] if "." in qual else ""
                target = pm.method_ids.get(cls_qual, {}).get(name)
            if target:
                add_edge("CALLS", "Function", caller, "Function", target, src)

    nodes_by_label: list[tuple[str, dict[str, UpsertNode]]] = [
        ("Repo", repos),
        ("Module", {str(pm.module.key): pm.module for pm in parsed_modules}),
        ("Class", {str(n.key): n for pm in parsed_modules for n in pm.classes}),
        ("Function", {str(n.key): n for pm in parsed_modules for n in pm.functions}),
        (
            "TerraformModuleCall",
            {
                str(n.key): n
                for pm in parsed_modules
                for n in pm.terraform_module_calls
            },
        ),
    ]
    counts: dict[str, int] = {}
    desired_by_label_source: dict[str, dict[str, set[str]]] = {
        "Module": {},
        "Class": {},
        "Function": {},
        "TerraformModuleCall": {},
    }
    for label, nodes in nodes_by_label:
        counts[label] = len(nodes)
        to_write = list(nodes.values())
        if label in desired_by_label_source:
            for node in nodes.values():
                source = str(node.source)
                desired_by_label_source[label].setdefault(source, set()).add(
                    str(node.key)
                )
            to_write = [n for n in to_write if str(n.source) in changed_sources]
        if to_write:
            summary = upsert_nodes(engine, config, UpsertNodesRequest(nodes=to_write))
            warnings.extend(summary.warnings)
    _record_ingest_hashes(engine, new_hashes)

    # Pruning and edge writes are scoped to changed + deleted files. An edge
    # is (re)written when its own file changed OR its target's file changed:
    # a symbol added to B that an unchanged A already referenced gains its
    # edge without rewriting all of A.
    authoritative_sources = changed_sources | _missing_module_sources(
        engine, directory_roots
    )
    module_source = {mid: str(pm.module.source) for mid, pm in by_module.items()}

    def owning_source(key: str) -> str | None:
        return module_source.get(key.split("#", 1)[0])

    live_edges = [
        edge
        for edge in edges.values()
        if str(edge.source) in changed_sources
        or owning_source(str(edge.to_key)) in changed_sources
    ]
    desired_edges = {
        (edge.type, str(edge.from_key), str(edge.to_key), str(edge.source))
        for edge in edges.values()
    }
    edges_pruned = _prune_code_edges(
        engine,
        authoritative_sources=authoritative_sources,
        desired=desired_edges,
    )
    nodes_pruned = _prune_code_nodes(
        engine,
        authoritative_sources=authoritative_sources,
        desired_by_label_source=desired_by_label_source,
    )
    nodes_pruned += _prune_legacy_repos(engine, repos)

    edges_by_type: dict[str, list[UpsertEdge]] = {}
    for edge in live_edges:
        edges_by_type.setdefault(edge.type, []).append(edge)
    edge_count = 0
    for rel_type in sorted(edges_by_type):
        batch = edges_by_type[rel_type]
        edge_count += len(batch)
        summary = upsert_edges(engine, config, UpsertEdgesRequest(edges=batch))
        warnings.extend(summary.warnings)

    if config.embedder is not None:
        from grag.embedworker import notify_embed_worker

        notify_embed_worker(engine)

    return CodeIngestResponse(
        repos=counts["Repo"],
        modules=counts["Module"],
        classes=counts["Class"],
        functions=counts["Function"],
        module_calls=counts["TerraformModuleCall"],
        edges=len(edges),
        nodes_pruned=nodes_pruned,
        edges_pruned=edges_pruned,
        files_parsed=len(successful_sources),
        files_unchanged=len(successful_sources) - len(changed_sources),
        warnings=warnings,
    )


# --- public: ingest_code_paths (CLI) ------------------------------------------------


def ingest_code_paths(
    config: GragConfig,
    paths: list[Path],
    *,
    calls: bool = True,
    max_file_kb: int = 1024,
) -> str:
    """Ingest code structure from `paths` and return a human-readable summary
    for the CLI (mirrors loaders.ingest_paths)."""
    from grag.service import GragService

    service = GragService(config)
    try:
        resp = service.ingest_code(
            CodeIngestRequest(
                paths=[str(p) for p in paths], calls=calls, max_file_kb=max_file_kb
            )
        )
    finally:
        service.close()

    lines = [
        (
            f"Ingested code from {len(paths)} path(s): "
            f"{resp.repos} repo(s), {resp.modules} module(s), {resp.classes} class(es), "
            f"{resp.functions} function(s), {resp.edges} edge(s) resolved; "
            f"{resp.files_unchanged}/{resp.files_parsed} file(s) unchanged (skipped); "
            f"{resp.nodes_pruned} stale node(s) and {resp.edges_pruned} stale edge(s) "
            f"pruned in {config.db_path}."
        )
    ]
    if resp.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in resp.warnings)
    return "\n".join(lines)
