"""Spec-driven tree-sitter walkers for the long tail of languages.

`code_ts.py` hand-writes one walker per language for TypeScript/JavaScript,
C#, Terraform and Go, each tuned to that grammar's quirks. Every further
language would cost another 100-line walker, so the ones added here share one
generic walker driven by a small ``LangSpec``: which node types are classes,
functions and namespaces, where their names live, and how imports are
spelled. Grammars come from ``tree-sitter-language-pack`` (one wheel, every
language, versions that match the installed tree-sitter runtime — the
per-grammar PyPI wheels drift out of ABI sync; the Kotlin one cannot parse a
class at all).

Coverage per language is deliberately "structure a reader would cite":
classes / structs / traits / interfaces / enums / records / modules-as-classes
(Ruby), functions and methods with their signature and doc comment, module
IMPORTS where the language has a resolvable notion of one. CALLS and INHERITS
stay Python-only, as for the other tree-sitter languages.

Partial parses are tolerated: tree-sitter recovers from a construct the
grammar does not know (SQL ``CREATE PROCEDURE`` in some dialects, a Kotlin
edge case) and still yields the rest of the file, so an unknown construct
costs one symbol, not the whole module. The strict "skip the file on any
error" rule keeps applying to the hand-written walkers.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grag.ingest.code import _ParsedModule
from grag.ingest.code_ts import (
    _PACK_SUFFIXES,
    _add_class,
    _add_function,
    _path_import_refs,
    _text,
    _walk_all,
)

_NAME_TYPES = frozenset(
    {
        "identifier",
        "type_identifier",
        "simple_identifier",
        "name",
        "constant",
        "word",
        "namespace_identifier",
        "field_identifier",
    }
)

NameFn = Callable[[Any, bytes], str | None]
ImportFn = Callable[[Any, bytes, "_ParsedModule", str], None]


def _default_name(node: Any, src: bytes) -> str | None:
    named = node.child_by_field_name("name")
    if named is not None:
        if named.type in _NAME_TYPES:
            return _text(named, src)
        # e.g. Swift `extension Greeter`: name=user_type > type_identifier
        for inner in _walk_all(named):
            if inner.type in _NAME_TYPES:
                return _text(inner, src)
    for child in node.named_children:
        if child.type in _NAME_TYPES:
            return _text(child, src)
    return None


@dataclass(frozen=True)
class LangSpec:
    language: str
    pack_name: str  # tree_sitter_language_pack grammar name
    class_types: frozenset[str]
    function_types: frozenset[str]
    # Namespace-like containers: descend, extending the qualname prefix.
    container_types: frozenset[str] = frozenset()
    # Wrappers to look through without touching the prefix (template
    # declarations, SQL `statement`, export wrappers).
    transparent_types: frozenset[str] = frozenset()
    # Containers that ADD methods to a type declared elsewhere (Rust `impl`,
    # Swift `extension`): methods get `Type.name` qualnames and link to the
    # Class only when it was declared in this file.
    impl_types: frozenset[str] = frozenset()
    body_fields: tuple[str, ...] = ("body",)
    name_of: NameFn = _default_name
    impl_name_of: NameFn = _default_name
    imports: ImportFn | None = None
    # Extensions stripped from path-style import specifiers.
    import_exts: tuple[str, ...] = ()
    aliases: Callable[[Any, bytes, str], list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# --- per-language name extractors -------------------------------------------------


def _c_declarator_name(node: Any, src: bytes) -> str | None:
    """function_definition -> the identifier inside (pointer_)declarator chains;
    C++ `Greeter::greet` keeps the qualified form."""
    decl = node.child_by_field_name("declarator")
    while decl is not None and decl.type not in (
        "identifier",
        "field_identifier",
        "qualified_identifier",
        "operator_name",
        "destructor_name",
    ):
        decl = decl.child_by_field_name("declarator")
    return _text(decl, src) if decl is not None else None


def _lua_name(node: Any, src: bytes) -> str | None:
    if node.type == "function_declaration":
        name = node.child_by_field_name("name")
        return _text(name, src).replace(":", ".") if name is not None else None
    # `M.f = function(...)` / `local f = function(...)`: the assignment's
    # `name` field is the first target (identifier or dot/method index).
    target = node.child_by_field_name("name")
    if target is None:
        targets = next((c for c in node.named_children if c.type == "variable_list"), None)
        if targets is None or not targets.named_children:
            return None
        target = targets.named_children[0]
    return _text(target, src).replace(":", ".")


def _lua_is_function_assignment(node: Any) -> bool:
    values = next((c for c in node.named_children if c.type == "expression_list"), None)
    return bool(
        values
        and values.named_children
        and values.named_children[0].type == "function_definition"
    )


def _swift_init_name(node: Any, src: bytes) -> str | None:
    return "init" if node.type == "init_declaration" else _default_name(node, src)


def _sql_name(node: Any, src: bytes) -> str | None:
    ref = next((c for c in node.named_children if c.type == "object_reference"), None)
    return _text(ref, src) if ref is not None else None


# --- per-language import extractors -----------------------------------------------


def _bash_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type != "command":
            continue
        name = node.child_by_field_name("name")
        if name is None or _text(name, src) not in ("source", "."):
            continue
        arg = node.child_by_field_name("argument")
        if arg is not None:
            spec = _text(arg, src).strip("\"'")
            if not spec.startswith("."):
                spec = "./" + spec  # `source lib/x.sh` is relative to cwd/file
            parsed.import_refs.extend(_path_import_refs(spec, rel_path, index_name=None))


def _dotted_imports(node_type: str, strip: tuple[str, ...] = ()) -> ImportFn:
    """Imports spelled as a dotted/qualified name: Java, Kotlin, Scala, Swift."""

    def collect(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
        for node in _walk_all(root):
            if node.type != node_type:
                continue
            text = _text(node, src).strip().rstrip(";")
            for prefix in ("import static ", "import ", *strip):
                text = text.removeprefix(prefix)
            text = text.split(" as ")[0].split("{")[0].strip()
            text = re.sub(r"\.\*$|\._$", "", text).replace("::", ".").replace("\\", ".")
            if text:
                parsed.import_refs.append(text)
                # `import a.b.C` may name a class inside module a.b
                if "." in text:
                    parsed.import_refs.append(text.rsplit(".", 1)[0])

    return collect


def _rust_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg is None:
                continue
            text = _text(arg, src).replace("::", ".")
            for prefix in ("crate.", "self.", "super."):
                text = text.removeprefix(prefix)
            text = text.split("{")[0].rstrip(".")
            if text:
                parsed.import_refs.append(text)
                if "." in text:
                    parsed.import_refs.append(text.rsplit(".", 1)[0])
        elif node.type == "mod_item" and node.child_by_field_name("body") is None:
            name = _default_name(node, src)
            if name:
                parsed.import_refs.extend(
                    _path_import_refs(f"./{name}", rel_path, index_name="mod")
                )


def _include_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type != "preproc_include":
            continue
        path = node.child_by_field_name("path")
        if path is None or path.type != "string_literal":
            continue  # <system> headers are external
        spec = _text(path, src).strip('"')
        if not spec.startswith("."):
            spec = "./" + spec
        parsed.import_refs.extend(_path_import_refs(spec, rel_path, index_name=None))


def _ruby_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type != "call":
            continue
        method = node.child_by_field_name("method")
        args = node.child_by_field_name("arguments")
        if method is None or args is None or not args.named_children:
            continue
        kind = _text(method, src)
        if kind not in ("require", "require_relative"):
            continue
        first = args.named_children[0]
        if first.type != "string":
            continue
        spec = _text(first, src).strip("\"'")
        if kind == "require_relative" and not spec.startswith("."):
            spec = "./" + spec
        parsed.import_refs.extend(_path_import_refs(spec, rel_path, index_name=None))


def _php_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type == "namespace_use_clause":
            qn = next((c for c in node.named_children if c.type == "qualified_name"), None)
            text = _text(qn if qn is not None else node, src).replace("\\", ".").strip(".")
            text = text.split(" as ")[0].strip()
            if text:
                parsed.import_refs.append(text)
        elif node.type.endswith(("require_expression", "require_once_expression",
                                 "include_expression", "include_once_expression")):
            string = next(
                (c for c in _walk_all(node) if c.type in ("string", "encapsed_string")),
                None,
            )
            if string is None:
                continue
            spec = _text(string, src).strip("\"'").lstrip("/")
            if spec and not spec.startswith("."):
                spec = "./" + spec
            parsed.import_refs.extend(_path_import_refs(spec, rel_path, index_name=None))


def _lua_imports(root: Any, src: bytes, parsed: _ParsedModule, rel_path: str) -> None:
    for node in _walk_all(root):
        if node.type != "function_call":
            continue
        name = node.child_by_field_name("name")
        if name is None or _text(name, src) != "require":
            continue
        string = next((c for c in _walk_all(node) if c.type == "string"), None)
        if string is not None:
            parsed.import_refs.append(_text(string, src).strip("\"'()").replace("/", "."))


# --- package aliases (module-index keys beyond the path-derived dotted name) -----


def _package_alias(node_type: str) -> Callable[[Any, bytes, str], list[str]]:
    def collect(root: Any, src: bytes, dotted: str) -> list[str]:
        stem = dotted.rsplit(".", 1)[-1]
        out: list[str] = []
        for node in root.named_children:
            if node.type == node_type:
                pkg = _text(node, src).strip().rstrip(";")
                pkg = re.sub(r"^(package|namespace)\s+", "", pkg).replace("\\", ".")
                if pkg:
                    out.append(f"{pkg}.{stem}")
                break
        return out

    return collect


# --- the specs ---------------------------------------------------------------------

SPECS: dict[str, LangSpec] = {
    "bash": LangSpec(
        language="bash",
        pack_name="bash",
        class_types=frozenset(),
        function_types=frozenset({"function_definition"}),
        imports=_bash_imports,
        import_exts=(".sh", ".bash"),
    ),
    "java": LangSpec(
        language="java",
        pack_name="java",
        class_types=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            }
        ),
        function_types=frozenset({"method_declaration", "constructor_declaration"}),
        imports=_dotted_imports("import_declaration"),
        aliases=_package_alias("package_declaration"),
    ),
    "kotlin": LangSpec(
        language="kotlin",
        pack_name="kotlin",
        class_types=frozenset({"class_declaration", "object_declaration"}),
        function_types=frozenset({"function_declaration", "secondary_constructor"}),
        container_types=frozenset({"companion_object"}),
        imports=_dotted_imports("import_header"),
        aliases=_package_alias("package_header"),
        extra={"class_body": "class_body"},
    ),
    "rust": LangSpec(
        language="rust",
        pack_name="rust",
        class_types=frozenset({"struct_item", "enum_item", "trait_item", "union_item"}),
        function_types=frozenset({"function_item", "function_signature_item"}),
        container_types=frozenset({"mod_item"}),
        impl_types=frozenset({"impl_item"}),
        impl_name_of=lambda n, s: (
            _text(n.child_by_field_name("type"), s).split("<")[0]
            if n.child_by_field_name("type") is not None
            else None
        ),
        imports=_rust_imports,
    ),
    "c": LangSpec(
        language="c",
        pack_name="c",
        class_types=frozenset({"struct_specifier", "union_specifier", "enum_specifier"}),
        function_types=frozenset({"function_definition"}),
        name_of=lambda n, s: (
            _c_declarator_name(n, s) if n.type == "function_definition" else _default_name(n, s)
        ),
        imports=_include_imports,
        import_exts=(".h", ".c"),
    ),
    "cpp": LangSpec(
        language="cpp",
        pack_name="cpp",
        class_types=frozenset(
            {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}
        ),
        function_types=frozenset({"function_definition"}),
        container_types=frozenset({"namespace_definition"}),
        transparent_types=frozenset({"template_declaration", "linkage_specification"}),
        name_of=lambda n, s: (
            _c_declarator_name(n, s) if n.type == "function_definition" else _default_name(n, s)
        ),
        imports=_include_imports,
        import_exts=(".hpp", ".hh", ".hxx", ".h", ".cpp", ".cc", ".cxx"),
    ),
    "ruby": LangSpec(
        language="ruby",
        pack_name="ruby",
        class_types=frozenset({"class", "module"}),
        function_types=frozenset({"method", "singleton_method"}),
        imports=_ruby_imports,
        import_exts=(".rb",),
    ),
    "php": LangSpec(
        language="php",
        pack_name="php",
        class_types=frozenset(
            {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}
        ),
        function_types=frozenset({"function_definition", "method_declaration"}),
        container_types=frozenset({"namespace_definition"}),
        imports=_php_imports,
        aliases=_package_alias("namespace_definition"),
        import_exts=(".php",),
    ),
    "swift": LangSpec(
        language="swift",
        pack_name="swift",
        class_types=frozenset({"class_declaration", "protocol_declaration"}),
        function_types=frozenset(
            {"function_declaration", "init_declaration", "protocol_function_declaration", "deinit_declaration"}
        ),
        name_of=_swift_init_name,
        imports=_dotted_imports("import_declaration"),
        extra={"extension_kind": "extension"},
    ),
    "lua": LangSpec(
        language="lua",
        pack_name="lua",
        class_types=frozenset(),
        function_types=frozenset({"function_declaration"}),
        transparent_types=frozenset({"variable_declaration"}),
        name_of=_lua_name,
        imports=_lua_imports,
        extra={"assignment": "assignment_statement"},
    ),
    "scala": LangSpec(
        language="scala",
        pack_name="scala",
        class_types=frozenset({"class_definition", "object_definition", "trait_definition"}),
        function_types=frozenset({"function_definition", "function_declaration"}),
        imports=_dotted_imports("import_declaration"),
        aliases=_package_alias("package_clause"),
    ),
    "sql": LangSpec(
        language="sql",
        pack_name="sql",
        class_types=frozenset({"create_table"}),
        function_types=frozenset({"create_function", "create_procedure", "create_view", "create_trigger"}),
        transparent_types=frozenset({"statement"}),
        name_of=_sql_name,
    ),
}

SUFFIXES: dict[str, str] = dict(_PACK_SUFFIXES)


# --- generic walker ------------------------------------------------------------------


def _class_body(node: Any, spec: LangSpec) -> Any | None:
    for f in spec.body_fields:
        body = node.child_by_field_name(f)
        if body is not None:
            return body
    wanted = spec.extra.get("class_body")
    for child in node.named_children:
        if child.type == wanted or child.type.endswith(("_body", "_list", "body_statement")):
            return child
    return None


def walk(spec: LangSpec, root: Any, src: bytes, parsed: _ParsedModule, path: Path, rel_path: str) -> None:
    language = spec.language
    source_path = str(path)

    def add_class(node: Any, name: str, prefix: str) -> str:
        qual = f"{prefix}.{name}" if prefix else name
        _add_class(parsed, node, node, src, name=name, qual=qual, rel_path=rel_path,
                   language=language, source_path=source_path)
        return qual

    module_id = str(parsed.module.key)

    def class_qual(simple: str) -> str | None:
        """Qualname of a class declared in this file, by simple name."""
        cid = parsed.class_ids.get(simple)
        return cid.split("#", 1)[1] if cid else None

    def add_function(node: Any, name: str, prefix: str, parent: str | None, link: bool) -> None:
        qual = f"{prefix}.{name}" if prefix else name
        # Never emit CONTAINS_CLASS_FUNCTION to a class this file did not declare.
        link = link and parent is not None and f"{module_id}#{parent}" in parsed.class_ids.values()
        _add_function(parsed, node, node, src, name=name, qual=qual, parent_class=parent,
                      rel_path=rel_path, language=language, source_path=source_path,
                      link_to_class=link)

    def visit(nodes: list[Any], prefix: str, parent: str | None, link: bool) -> None:
        for node in nodes:
            t = node.type
            if t in spec.transparent_types:
                visit(node.named_children, prefix, parent, link)
            elif t in spec.class_types:
                name = spec.name_of(node, src)
                if not name:
                    continue
                if spec.extra.get("extension_kind") and _swift_kind(node, src) == "extension":
                    # Swift `extension T { ... }`: methods of T, declared elsewhere
                    _visit_impl(node, name, prefix)
                    continue
                qual = add_class(node, name, prefix)
                body = _class_body(node, spec)
                if body is not None:
                    visit(body.named_children, qual, qual, True)
            elif t in spec.impl_types:
                name = spec.impl_name_of(node, src)
                if name:
                    _visit_impl(node, name, prefix)
            elif t in spec.function_types:
                name = spec.name_of(node, src)
                if not name:
                    continue
                if "::" in name:  # C++ out-of-line `T::f` definition
                    owner, _, short = name.rpartition("::")
                    owner_qual = class_qual(owner.split("::")[-1]) or owner.replace("::", ".")
                    add_function(node, short, owner_qual, owner_qual, True)
                    continue
                if "." in name and parent is None:  # Lua `M.f` / `M:f`
                    owner, _, short = name.rpartition(".")
                    add_function(node, short, owner, None, True)
                    continue
                add_function(node, name, prefix, parent, link)
            elif t in spec.container_types:
                name = spec.name_of(node, src)
                body = _class_body(node, spec)
                nested = f"{prefix}.{name}" if prefix and name else (name or prefix)
                children = body.named_children if body is not None else node.named_children
                # a Kotlin companion object keeps the enclosing class as parent
                keep_parent = parent if t == "companion_object" else None
                visit(children, nested if t != "companion_object" else prefix, keep_parent, link)
            elif t == spec.extra.get("assignment") and _lua_is_function_assignment(node):
                name = _lua_name(node, src)
                if name:
                    owner, _, short = name.rpartition(".")
                    add_function(node, short, owner or prefix, None, True)

    def _visit_impl(node: Any, type_name: str, prefix: str) -> None:
        body = _class_body(node, spec)
        if body is None:
            return
        owner = class_qual(type_name) or (f"{prefix}.{type_name}" if prefix else type_name)
        visit(body.named_children, owner, owner, True)

    visit(root.named_children, "", None, True)
    if spec.imports is not None:
        spec.imports(root, src, parsed, rel_path)
    if spec.aliases is not None:
        parsed.aliases.extend(spec.aliases(root, src, parsed.dotted))


def _swift_kind(node: Any, src: bytes) -> str | None:
    kind = node.child_by_field_name("declaration_kind")
    return _text(kind, src) if kind is not None else None


def dotted_module_name(rel_path: str, suffix: str, language: str) -> str:
    """Path-derived module key, with language-specific index-file conventions."""
    dotted = rel_path[: -len(suffix)].replace("/", ".")
    if language == "rust":
        dotted = dotted.removesuffix(".mod")
    return dotted
