"""Tree-sitter parsers for the code graph (Wave B): TypeScript/JavaScript,
C#, Terraform/HCL and Go. Python stays on stdlib ast in `grag.ingest.code`.

This module is imported LAZILY from `grag.ingest.code` (which owns the
`_PARSERS` dispatch) so a base install without the `code` extra never imports
tree-sitter. Parsers fill the same `_ParsedModule` contract as
`_parse_python` — Module/Class/Function nodes, CONTAINS_* rels, import refs —
so the walk/upsert pipeline remains shared. JS/TS additionally produce lexical
binding summaries for `code_js` to resolve calls, runtime class extends and
relative imports. Go delegates package calls, constants and basic interface method
sets to code_go. C# namespaces and Terraform directories retain best-effort imports.

Grammar notes: `.tsx` uses the tsx language (JSX on) and plain `.ts` the
typescript language (JSX must stay off there), per tree-sitter-typescript.
Terraform has no class/function/import declarations, so .tf files yield a
Module node, a TerraformModuleCall node per `module` block (name/source/
version read straight off the block — never hand-typed, so a version pin in
the graph can't drift from the source of truth), and IMPORTS edges from
local `module` block sources only. Go has no lexical class nesting: methods
are top-level funcs carrying a receiver type, matched back to their
struct/interface Class node through the isolated package index in code_go.
"""

from __future__ import annotations

import importlib
import logging
import posixpath
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from grag.core.errors import ConfigurationError
from grag.core.types import UpsertNode
from grag.ingest.code import _ParsedModule

_INSTALL_HINT = 'Install code parsing: pip install "gragdb[code]"'

# suffix -> (grammar module, language-factory attribute, graph language prop)
_SUFFIX_LANGUAGES = {
    ".ts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".mts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx", "typescript"),
    ".js": ("tree_sitter_javascript", "language", "javascript"),
    ".jsx": ("tree_sitter_javascript", "language", "javascript"),
    ".mjs": ("tree_sitter_javascript", "language", "javascript"),
    ".cjs": ("tree_sitter_javascript", "language", "javascript"),
    ".cs": ("tree_sitter_c_sharp", "language", "csharp"),
    ".tf": ("tree_sitter_hcl", "language", "hcl"),
    ".go": ("tree_sitter_go", "language", "go"),
}
# The long tail comes from tree-sitter-language-pack and the spec-driven
# walker in grag.ingest.code_langs (imported lazily in parse_file — it
# imports helpers from here); "pack" marks the grammar source.
_PACK_SUFFIXES: dict[str, str] = {
    ".sh": "bash", ".bash": "bash",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".lua": "lua",
    ".scala": "scala", ".sc": "scala",
    ".sql": "sql",
}
for _suffix, _language in _PACK_SUFFIXES.items():
    _SUFFIX_LANGUAGES[_suffix] = ("pack", _language, _language)
# Vue single-file components: the <script> block parses as TS or JS.
for _framework in (".vue", ".svelte", ".astro"):
    _SUFFIX_LANGUAGES[_framework] = ("scripts", "", _framework[1:])

# Import specifiers may carry an explicit extension; strip it to match the
# extension-less dotted keys modules are indexed under.
_CODE_EXTS = (
    ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".tf", ".json", ".vue",
    ".sh", ".bash", ".hpp", ".hh", ".hxx", ".h", ".cpp", ".cc", ".cxx", ".c",
    ".rb", ".php", ".rs", ".lua",
)

def _build_parser(suffix: str) -> Any:
    """Build a tree-sitter Parser for `suffix`, importing tree-sitter and the
    grammar wheel lazily. Without the `code` extra, raise ConfigurationError
    with the install hint (surfaces as ERROR+HINT through the tools)."""
    mod_name, factory, _ = _SUFFIX_LANGUAGES[suffix]
    if mod_name == "pack":
        try:
            from tree_sitter_language_pack import (
                cache_dir,
                downloaded_languages,
                get_parser,
            )
        except ImportError as exc:
            raise ConfigurationError(
                f"cannot parse '{suffix}' files: the tree-sitter code extra is not installed",
                hint=_INSTALL_HINT,
            ) from exc
        try:
            if factory not in downloaded_languages():
                logging.getLogger("grag").warning(
                    "Preparing %s code grammar: first use may download to %s.", factory, cache_dir(),
                )
            return get_parser(factory)  # type: ignore[arg-type]
        except Exception as exc:
            raise ConfigurationError(
                f"Cannot load '{factory}' grammar for '{suffix}' files: {exc}",
                hint="Check the tree-sitter-language-pack cache permissions and network access. "
                "Run grag doctor --prepare while online, then grag doctor to verify offline parsing. "
                "A cached library can still be incompatible or damaged.",
            ) from exc
    try:
        from tree_sitter import Language, Parser

        grammar = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"cannot parse '{suffix}' files: the tree-sitter code extra is not installed",
            hint=_INSTALL_HINT,
        ) from exc
    # Version-drift guard: 0.23+ grammar wheels expose language() returning a
    # PyCapsule that Language() wraps; some wheels return a ready Language.
    capsule = getattr(grammar, factory)()
    language = capsule if isinstance(capsule, Language) else Language(capsule)
    try:
        return Parser(language)  # tree-sitter >= 0.22
    except TypeError:  # older bindings: set_language on a bare Parser
        parser = Parser()
        parser.set_language(language)  # type: ignore[attr-defined]
        return parser


def parse_file(
    suffix: str, path: Path, source: str, *, repo: str, rel_path: str
) -> _ParsedModule:
    """Parse one ts/js/cs/tf file into a _ParsedModule.

    JS/TS call collection is separate from resolution, where `calls` is honored.
    Raises ValueError when tree-sitter reports syntax
    errors — the caller collects it as a warning and skips the file, exactly
    like SyntaxError for .py.
    """
    from grag.ingest import code_langs

    language = _SUFFIX_LANGUAGES[suffix][2]
    grammar_suffix = suffix
    if suffix in {".vue", ".svelte", ".astro"}:
        return _parse_scripts(suffix, path, source, repo=repo, rel_path=rel_path)
    spec = code_langs.SPECS.get(language)
    root = _build_parser(grammar_suffix).parse(source.encode("utf-8")).root_node
    if root.has_error and spec is None:
        raise ValueError("tree-sitter reported syntax errors")
    dotted = code_langs.dotted_module_name(rel_path, suffix, language)
    # TypeScript declaration file: foo.d.ts -> foo
    dotted = dotted.removesuffix(".d")
    parsed = _ParsedModule(
        module=UpsertNode(
            label="Module",
            key=f"{repo}:{rel_path}",
            properties={"path": rel_path, "language": language, "name": dotted},
            source=str(path),
        ),
        dotted=dotted,
    )
    if spec is not None:
        code_langs.walk(spec, root, source.encode("utf-8"), parsed, path, rel_path)
    elif language == "csharp":
        _walk_csharp(root, source.encode("utf-8"), parsed, path, rel_path)
    elif language == "hcl":
        _walk_hcl(root, source.encode("utf-8"), parsed, rel_path)
    elif language == "go":
        from grag.ingest.code_go import parse

        parsed.go = parse(root, source.encode("utf-8"), parsed, rel_path)
    else:
        from grag.ingest.code_js import analyze

        symbols = _walk_tsjs(root, source.encode("utf-8"), parsed, path, rel_path)
        parsed.js_units.append(analyze(root, source.encode("utf-8"), symbols))
    return parsed


def _parse_scripts(suffix: str, path: Path, source: str, *, repo: str, rel_path: str) -> _ParsedModule:
    from grag.ingest.code_js import analyze
    from grag.ingest.code_scripts import scripts

    blocks, omitted = scripts(source, suffix)
    parsed = _ParsedModule(
        module=UpsertNode(label="Module", key=f"{repo}:{rel_path}",
                          properties={"path": rel_path, "language": suffix[1:],
                                      "name": rel_path.rsplit(".", 1)[0].replace("/", ".")}, source=str(path)),
        dotted=rel_path.rsplit(".", 1)[0].replace("/", "."),
        framework=True, script_omissions=omitted,
    )
    for block in blocks:
        src = block.source.encode("utf-8")
        root = _build_parser(block.suffix).parse(src).root_node
        if root.has_error:
            raise ValueError(f"tree-sitter reported syntax errors in {block.prefix or 'script'} block")
        # Definition language describes the script, Module language its container.
        parsed.module.properties["language"] = _SUFFIX_LANGUAGES[block.suffix][2]
        symbols = _walk_tsjs(root, src, parsed, path, rel_path, prefix=block.prefix)
        parsed.js_units.append(analyze(root, src, symbols))
    parsed.module.properties["language"] = suffix[1:]
    return parsed


# --- shared node helpers ---------------------------------------------------------


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk_all(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from _walk_all(child)


def _signature(node: Any, src: bytes) -> str:
    """First line of the declaration, body trimmed — the tree-sitter analogue
    of code.py's ast-based signature reconstruction."""
    line = _text(node, src).split("\n", 1)[0].strip()
    brace = line.rfind("{")  # one-line decls: drop the inline body
    if brace != -1:
        line = line[:brace].rstrip()
    return line.rstrip(";").rstrip()


_XML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")  # C# xmldoc <summary> etc.
_COMMENT_TYPES = frozenset({"comment", "block_comment", "line_comment"})


def _clean_comment(raw: str) -> str:
    """Strip comment markers: // and /// lines, /* */ and JSDoc leading *."""
    lines = []
    for raw_line in raw.split("\n"):
        cleaned = raw_line.strip()
        cleaned = re.sub(r"^/{2,}|^#+|^--+", "", cleaned)
        cleaned = re.sub(r"^/\*+", "", cleaned)
        cleaned = re.sub(r"\*/$", "", cleaned)
        cleaned = re.sub(r"^\*( |$)", "", cleaned)
        lines.append(cleaned.strip())
    return "\n".join(lines).strip()


def _docstring(anchor: Any, src: bytes) -> str:
    """Leading comment block directly above the declaration, if trivially
    captured, else ''. Comments must be line-adjacent (a blank line breaks
    the block, so a file-header comment doesn't glue onto a doc comment).
    For export-wrapped declarations `anchor` is the export_statement, which
    carries the comment."""
    comments = []
    sib = anchor.prev_named_sibling
    next_start = anchor.start_point[0]
    while sib is not None and sib.type in _COMMENT_TYPES:
        # Some grammars (Rust line comments) include the trailing newline, so
        # the comment "ends" at column 0 of the next row.
        end_row = sib.end_point[0] - (1 if sib.end_point[1] == 0 else 0)
        if end_row != next_start - 1:
            break
        comments.append(_clean_comment(_text(sib, src)))
        next_start = sib.start_point[0]
        sib = sib.prev_named_sibling
    if not comments:
        return ""
    return _XML_TAG.sub("", "\n".join(reversed(comments))).strip()


def _add_class(
    parsed: _ParsedModule,
    node: Any,
    anchor: Any,
    src: bytes,
    *,
    name: str,
    qual: str,
    rel_path: str,
    language: str,
    source_path: str,
) -> None:
    """Class node + CONTAINS_MODULE_CLASS, mirroring code._parse_python (which
    also attaches nested classes to the Module: no Class->Class rel exists)."""
    module_id = str(parsed.module.key)
    cid = f"{module_id}#{qual}"
    parsed.classes.append(
        UpsertNode(
            label="Class",
            key=cid,
            properties={
                "name": name,
                "path": rel_path,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _signature(node, src),
                "docstring": _docstring(anchor, src),
                "language": language,
            },
            source=source_path,
        )
    )
    parsed.contains.append(("CONTAINS_MODULE_CLASS", module_id, cid))
    parsed.class_ids.setdefault(name, cid)


def _add_function(
    parsed: _ParsedModule,
    node: Any,
    anchor: Any,
    src: bytes,
    *,
    name: str,
    qual: str,
    parent_class: str | None,
    rel_path: str,
    language: str,
    source_path: str,
    link_to_class: bool = True,
) -> str:
    """Function node + CONTAINS_MODULE_FUNCTION / CONTAINS_CLASS_FUNCTION,
    mirroring code._parse_python (is_method when inside a class). Returns
    the function's id.

    link_to_class=False still sets is_method and method_ids but skips the
    same-file CONTAINS_CLASS_FUNCTION append — for Go, whose methods can
    name a receiver type declared in a different file of the same package;
    the Go package resolver supplies those containment links
    instead (a same-file class id would often be wrong)."""
    module_id = str(parsed.module.key)
    fid = f"{module_id}#{qual}"
    signature = _signature(node, src)
    identity: dict[str, str] = {}
    if language in {"java", "csharp"}:
        from grag.ingest.code_identity import function_identity

        fid, identity_signature, signature = function_identity(node, src, fid)
        identity["identity_signature"] = identity_signature
    is_method = parent_class is not None
    parsed.functions.append(
        UpsertNode(
            label="Function",
            key=fid,
            properties={
                "name": name,
                "path": rel_path,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": signature,
                "docstring": _docstring(anchor, src),
                "language": language,
                "is_method": is_method,
                **identity,
            },
            source=source_path,
        )
    )
    if parent_class is not None:
        if link_to_class:
            parsed.contains.append(
                ("CONTAINS_CLASS_FUNCTION", f"{module_id}#{parent_class}", fid)
            )
        parsed.method_ids.setdefault(parent_class, {})[name] = fid
    else:
        parsed.contains.append(("CONTAINS_MODULE_FUNCTION", module_id, fid))
        if "." not in qual:
            parsed.func_ids.setdefault(name, fid)
    return fid


def _path_import_refs(
    spec: str, rel_path: str, *, index_name: str | None = "index"
) -> list[str]:
    """Normalize an import specifier to candidate module-index keys.

    Relative specifiers ('./lib/x', '../pkg') resolve against the importing
    file's directory; 'dir' imports also try '<dir>.index'. Bare package
    names ('lodash', '@scope/pkg') become dotted candidates that only match
    same-tree modules by unique suffix (monorepos); external packages resolve
    to nothing and are skipped silently.
    """
    if not spec:
        return []
    if not spec.startswith("."):
        return [spec.replace("/", ".")]
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel_path), spec))
    if joined.startswith(".."):  # escapes the scanned repo root
        return []
    for ext in _CODE_EXTS:
        if joined.endswith(ext):
            joined = joined[: -len(ext)]
            break
    joined = joined.removesuffix(".d")  # foo.d.ts
    dotted = joined.replace("/", ".")
    refs = [dotted]
    if index_name:
        refs.append(f"{dotted}.{index_name}")
    return refs


def _string_value(node: Any, src: bytes) -> str:
    """String literal content without quotes (ts string_fragment / hcl
    template_literal / go interpreted_string_literal_content)."""
    for child in _walk_all(node):
        if child.type in (
            "string_fragment",
            "template_literal",
            "interpreted_string_literal_content",
        ):
            return _text(child, src)
    return _text(node, src).strip("\"'")


# --- typescript / javascript -------------------------------------------------------

_TSJS_CLASS_TYPES = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration"}
)
_TSJS_FUNCTION_TYPES = frozenset(
    {"function_declaration", "generator_function_declaration"}
)
_TSJS_METHOD_TYPES = frozenset(
    {"method_definition", "abstract_method_signature", "method_signature"}
)
_TSJS_FUNC_EXPR_TYPES = frozenset(
    {"arrow_function", "function_expression", "generator_function"}
)
_TSJS_NAMESPACE_TYPES = frozenset({"internal_module", "module"})
_TSJS_DECL_TYPES = (
    _TSJS_CLASS_TYPES
    | _TSJS_FUNCTION_TYPES
    | frozenset({"lexical_declaration", "variable_declaration"})
    | _TSJS_NAMESPACE_TYPES
)


def _walk_tsjs(
    root: Any, src: bytes, parsed: _ParsedModule, path: Path, rel_path: str, *, prefix: str = ""
) -> dict[int, tuple[str, str]]:
    language = str(parsed.module.properties["language"])
    source_path = str(path)
    symbols: dict[int, tuple[str, str]] = {}

    def name_of(node: Any) -> str | None:
        name = node.child_by_field_name("name")
        return _text(name, src) if name is not None else None

    def visit(stmts: list[Any], prefix: str) -> None:
        """Top-level (and namespace-nested) declarations. Declarations nested
        inside function bodies are skipped — 'where straightforward' only."""
        for node in stmts:
            anchor, target = node, node
            if node.type == "export_statement":
                # unwrap: `export class X` / `export const f = ...`; a bare
                # `export { a } from './m'` has no decl (its source is an
                # import ref, collected by the whole-tree pass below)
                target = next(
                    (c for c in node.children if c.type in _TSJS_DECL_TYPES), None
                )
                if target is None:
                    continue
            elif (
                node.type == "expression_statement"
                and node.named_children
                and node.named_children[0].type in _TSJS_NAMESPACE_TYPES
            ):
                # tree-sitter-typescript wraps `namespace N {}` this way
                target = node.named_children[0]
            if target.type in _TSJS_CLASS_TYPES:
                name = name_of(target)
                if not name:
                    continue
                qual = f"{prefix}.{name}" if prefix else name
                _add_class(
                    parsed,
                    target,
                    anchor,
                    src,
                    name=name,
                    qual=qual,
                    rel_path=rel_path,
                    language=language,
                    source_path=source_path,
                )
                symbols[target.id] = ("Class", f"{parsed.module.key}#{qual}")
                body = target.child_by_field_name("body")
                if body is not None:
                    for member in body.children:
                        if member.type in _TSJS_METHOD_TYPES:
                            mname = name_of(member)
                            if mname:
                                fid = _add_function(
                                    parsed,
                                    member,
                                    member,
                                    src,
                                    name=mname,
                                    qual=f"{qual}.{mname}",
                                    parent_class=qual,
                                    rel_path=rel_path,
                                    language=language,
                                    source_path=source_path,
                                )
                                symbols[member.id] = ("Function", fid)
            elif target.type in _TSJS_FUNCTION_TYPES:
                name = name_of(target)
                if name:
                    qual = f"{prefix}.{name}" if prefix else name
                    fid = _add_function(
                        parsed,
                        target,
                        anchor,
                        src,
                        name=name,
                        qual=qual,
                        parent_class=None,
                        rel_path=rel_path,
                        language=language,
                        source_path=source_path,
                    )
                    symbols[target.id] = ("Function", fid)
            elif target.type in ("lexical_declaration", "variable_declaration"):
                # `const f = (x) => ...` / `const f = function ...`
                for decl in target.named_children:
                    if decl.type != "variable_declarator":
                        continue
                    value = decl.child_by_field_name("value")
                    name_node = decl.child_by_field_name("name")
                    if (
                        value is None
                        or value.type not in _TSJS_FUNC_EXPR_TYPES
                        or name_node is None
                        or name_node.type != "identifier"  # destructuring
                    ):
                        continue
                    name = _text(name_node, src)
                    qual = f"{prefix}.{name}" if prefix else name
                    fid = _add_function(
                        parsed,
                        decl,
                        anchor,
                        src,
                        name=name,
                        qual=qual,
                        parent_class=None,
                        rel_path=rel_path,
                        language=language,
                        source_path=source_path,
                    )
                    symbols[value.id] = ("Function", fid)
            elif target.type in _TSJS_NAMESPACE_TYPES:
                ns = None
                body = None
                for child in target.named_children:
                    if ns is None and child.type in (
                        "identifier",
                        "property_identifier",
                        "string",
                    ):
                        ns = _text(child, src).strip("\"'")
                    elif child.type == "statement_block":
                        body = child
                if body is not None:
                    nested = f"{prefix}.{ns}" if prefix and ns else (ns or prefix)
                    visit(body.children, nested)

    program = root.named_children if root.type == "program" else root.children
    visit(list(program), prefix)

    return symbols


# --- c# ----------------------------------------------------------------------------

_CS_CLASS_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "record_declaration",
        "struct_declaration",
    }
)
_CS_METHOD_TYPES = frozenset({"method_declaration", "constructor_declaration"})


def _walk_csharp(
    root: Any, src: bytes, parsed: _ParsedModule, path: Path, rel_path: str
) -> None:
    language = "csharp"
    source_path = str(path)

    def name_of(node: Any) -> str | None:
        name = node.child_by_field_name("name")
        if name is None:  # e.g. qualified_name on namespaces
            name = next(
                (
                    c
                    for c in node.named_children
                    if c.type in ("identifier", "qualified_name")
                ),
                None,
            )
        return _text(name, src) if name is not None else None

    def visit_types(stmts: list[Any], prefix: str) -> None:
        for node in stmts:
            if node.type in _CS_CLASS_TYPES:
                name = name_of(node)
                if not name:
                    continue
                qual = f"{prefix}.{name}" if prefix else name
                _add_class(
                    parsed,
                    node,
                    node,
                    src,
                    name=name,
                    qual=qual,
                    rel_path=rel_path,
                    language=language,
                    source_path=source_path,
                )
                body = node.child_by_field_name("body")  # None for positional records
                if body is not None:
                    visit_types(body.children, qual)  # nested types
                    for member in body.children:
                        if member.type in _CS_METHOD_TYPES:
                            mname = name_of(member)
                            if mname:
                                _add_function(
                                    parsed,
                                    member,
                                    member,
                                    src,
                                    name=mname,
                                    qual=f"{qual}.{mname}",
                                    parent_class=qual,
                                    rel_path=rel_path,
                                    language=language,
                                    source_path=source_path,
                                )
            elif node.type == "namespace_declaration":
                ns = name_of(node)
                full = f"{prefix}.{ns}" if prefix and ns else (ns or prefix)
                if ns:
                    parsed.aliases.append(full)
                body = node.child_by_field_name("body")
                if body is not None:
                    visit_types(body.children, full)

    # A file-scoped namespace applies to everything after it (the following
    # declarations are its SIBLINGS, not children).
    prefix = ""
    for node in root.children:
        if node.type == "file_scoped_namespace_declaration":
            ns = name_of(node)
            if ns:
                prefix = ns
                parsed.aliases.append(ns)
    visit_types(root.children, prefix)

    # using directives, whole-tree: `using Foo.Bar;` / `using static F.B;` /
    # aliases (`using X = Foo.Bar;` resolves to the right-hand name).
    for node in _walk_all(root):
        if node.type != "using_directive":
            continue
        names = [
            c for c in node.named_children if c.type in ("qualified_name", "identifier")
        ]
        if names:
            parsed.import_refs.append(_text(names[-1], src))


# --- terraform / hcl -----------------------------------------------------------------


def _walk_hcl(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    """Terraform files have no class/function/import declarations: the
    structural facts are the Module node itself, a TerraformModuleCall node
    per `module` block (name/source/version, mechanically read off the block
    — the whole point being that an LLM never has to retype a version pin
    from a doc), and `module` block sources that reference local module
    DIRECTORIES. Each .tf file registers its directory as an alias, so a
    local source resolves when the directory holds exactly one .tf file
    (several files -> ambiguous -> skipped silently). Registry/git sources
    still get a TerraformModuleCall node; they just skip the IMPORTS edge
    (no scanned file to point at)."""
    module_id = str(parsed.module.key)
    dir_dotted = posixpath.dirname(rel_path).replace("/", ".")
    if dir_dotted:
        parsed.aliases.append(dir_dotted)
    for node in _walk_all(root):
        if node.type != "block":
            continue
        children = node.named_children
        if (
            not children
            or children[0].type != "identifier"
            or _text(children[0], src) != "module"
        ):
            continue
        label = children[1] if len(children) > 1 else None
        if label is None or label.type != "string_lit":
            continue  # malformed/labelless module block: nothing to key on
        body = next((c for c in children if c.type == "body"), None)
        if body is None:
            continue
        name = _string_value(label, src)
        source_val = ""
        version_val = ""
        for attr in body.named_children:
            if attr.type != "attribute" or not attr.named_children:
                continue
            attr_name = _text(attr.named_children[0], src)
            if attr_name == "source":
                source_val = _string_value(attr, src)
            elif attr_name == "version":
                version_val = _string_value(attr, src)
        call_id = f"{module_id}#{name}"
        parsed.terraform_module_calls.append(
            UpsertNode(
                label="TerraformModuleCall",
                key=call_id,
                properties={
                    "name": name,
                    "source": source_val,
                    "version": version_val,
                    "path": rel_path,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                },
                source=parsed.module.source,
            )
        )
        parsed.contains.append(("CONTAINS_MODULE_MODULECALL", module_id, call_id))
        # Local path sources ('./x', '../x') only; registry/git sources name
        # no scanned file and get no IMPORTS edge (the TerraformModuleCall
        # node above still records them).
        if source_val.startswith("."):
            parsed.import_refs.extend(
                _path_import_refs(source_val, rel_path, index_name=None)
            )
