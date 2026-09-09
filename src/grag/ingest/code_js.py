"""Conservative lexical JS/TS relationships. No runtime/type/bundler inference.

Resolve only named
functions/classes or static ES import/export bindings in the selected files.
Unknown bindings shadow known ones, and writes invalidate a binding for the whole
scope. This deliberately sacrifices recall to avoid inventing call targets.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grag.core.types import UpsertEdge

if TYPE_CHECKING:
    from grag.ingest.code import _ParsedModule

_FUNCTIONS = {
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "generator_function",
    "arrow_function",
    "method_definition",
}
_CLASSES = {"class_declaration", "abstract_class_declaration", "class"}
_IDENTIFIERS = {
    "identifier",
    "type_identifier",
    "shorthand_property_identifier_pattern",
}
_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode() if node is not None else ""


def _expression(node: Any, src: bytes) -> tuple[str, ...]:
    if node is None:
        return ()
    if node.type in {"identifier", "type_identifier"}:
        return (_text(node, src),)
    if node.type == "member_expression" and not any(
        c.type == "optional_chain" for c in node.children
    ):
        obj, prop = (
            node.child_by_field_name("object"),
            node.child_by_field_name("property"),
        )
        if (
            obj is not None
            and obj.type == "identifier"
            and prop is not None
            and prop.type == "property_identifier"
        ):
            return _text(obj, src), _text(prop, src)
    return ()


def _names(node: Any, src: bytes) -> list[str]:
    """Binding/assignment patterns only: never walk default values or type names."""
    if node is None:
        return []
    if node.type in _IDENTIFIERS:
        return [_text(node, src)]
    if node.type in {"required_parameter", "optional_parameter"}:
        return _names(node.child_by_field_name("pattern"), src)
    if node.type in {"assignment_pattern", "object_assignment_pattern"}:
        return _names(node.child_by_field_name("left"), src)
    if node.type == "pair_pattern":
        return _names(node.child_by_field_name("value"), src)
    if node.type in {
        "formal_parameters",
        "array_pattern",
        "object_pattern",
        "rest_pattern",
    }:
        return [name for c in node.named_children for name in _names(c, src)]
    return []


@dataclass
class Binding:
    label: str = ""
    key: str = ""
    spec: str = ""
    export: str = ""


@dataclass
class Scope:
    parent: Scope | None = None
    function: bool = False
    bindings: dict[str, Binding | None] = field(default_factory=dict)

    def declare(self, name: str, binding: Binding | None = None) -> None:
        # Duplicate declarations/overloads are ambiguous, even when valid TS.
        self.bindings[name] = None if name in self.bindings else binding

    def owner(self, name: str) -> Scope | None:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope
            scope = scope.parent
        return None

    def lookup(self, name: str) -> Binding | None:
        owner = self.owner(name)
        return owner.bindings[name] if owner is not None else None


@dataclass
class Reference:
    kind: str
    caller: str | None
    expression: tuple[str, ...]
    scope: Scope
    line: int
    end: int


@dataclass
class Unit:
    scope: Scope = field(default_factory=lambda: Scope(function=True))
    imports: list[tuple[str, int]] = field(default_factory=list)
    exports: dict[str, str | Binding | None] = field(default_factory=dict)
    refs: list[Reference] = field(default_factory=list)
    dynamic_scope: bool = False
    omissions: Counter[str] = field(default_factory=Counter)


def analyze(root: Any, src: bytes, symbols: dict[int, tuple[str, str]]) -> Unit:
    unit = Unit()

    def symbol(node: Any) -> Binding | None:
        match = symbols.get(node.id)
        return Binding(*match) if match is not None else None

    writes: list[tuple[Scope, Any]] = []
    requires: list[tuple[str, int, Scope]] = []

    def visit(node: Any, scope: Scope, caller: str | None) -> None:
        typ = node.type
        if typ == "import_statement":
            require_clause = next(
                (c for c in node.named_children if c.type == "import_require_clause"),
                None,
            )
            if require_clause is not None:
                scope.declare(_text(require_clause.named_children[0], src))
                unit.imports.append(
                    (
                        _text(require_clause.child_by_field_name("source"), src).strip(
                            "\"'"
                        ),
                        node.start_point[0] + 1,
                    )
                )
                unit.omissions["commonjs_binding"] += 1
                return
            source = _text(node.child_by_field_name("source"), src).strip("\"'")
            unit.imports.append((source, node.start_point[0] + 1))
            type_only = any(c.type == "type" for c in node.children)
            clause = next(
                (c for c in node.named_children if c.type == "import_clause"), None
            )
            for c in clause.named_children if clause else []:
                if c.type == "identifier":
                    scope.declare(
                        _text(c, src),
                        None if type_only else Binding(spec=source, export="default"),
                    )
                elif c.type == "namespace_import":
                    scope.declare(
                        _text(c.named_children[-1], src),
                        None if type_only else Binding(spec=source, export="*"),
                    )
                elif c.type == "named_imports":
                    for spec in c.named_children:
                        name = _text(spec.child_by_field_name("name"), src).strip("\"'")
                        alias = _text(spec.child_by_field_name("alias"), src) or name
                        only = type_only or any(n.type == "type" for n in spec.children)
                        scope.declare(
                            alias, None if only else Binding(spec=source, export=name)
                        )
            return
        if typ == "import_alias":
            scope.declare(_text(node.named_children[0], src))
            unit.omissions["typescript_import_alias"] += 1
            return
        if typ == "export_statement":
            source = _text(node.child_by_field_name("source"), src).strip("\"'")
            if source:
                unit.imports.append((source, node.start_point[0] + 1))
            declaration = node.child_by_field_name("declaration")
            clause = next(
                (c for c in node.named_children if c.type == "export_clause"), None
            )
            default = any(c.type == "default" for c in node.children)
            type_only = any(c.type == "type" for c in node.children)
            # Exports in namespaces/ambient modules aren't file exports.
            if scope is unit.scope:
                if declaration is not None:
                    names = _names(declaration.child_by_field_name("name"), src)
                    if declaration.type in {
                        "lexical_declaration",
                        "variable_declaration",
                    }:
                        names = [
                            n
                            for c in declaration.named_children
                            for n in _names(c.child_by_field_name("name"), src)
                        ]
                    for name in names:
                        unit.exports["default" if default else name] = (
                            None if type_only else name
                        )
                elif clause is not None:
                    for spec in clause.named_children:
                        name = _text(spec.child_by_field_name("name"), src).strip("\"'")
                        alias = (
                            _text(spec.child_by_field_name("alias"), src).strip("\"'")
                            or name
                        )
                        only = type_only or any(c.type == "type" for c in spec.children)
                        unit.exports[alias] = (
                            None
                            if only
                            else (Binding(spec=source, export=name) if source else name)
                        )
                elif default:
                    value = node.child_by_field_name("value")
                    unit.exports["default"] = (
                        _text(value, src)
                        if value is not None and value.type == "identifier"
                        else None
                    )
                    if unit.exports["default"] is None:
                        unit.omissions["anonymous_or_expression_export"] += 1
                else:
                    unit.omissions["wildcard_export"] += 1
            for child in node.named_children:
                if child != clause and child.type != "string":
                    visit(child, scope, caller)
            return
        if typ in {"lexical_declaration", "variable_declaration"}:
            target = scope
            if typ == "variable_declaration":
                while not target.function and target.parent is not None:
                    target = target.parent
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                value = decl.child_by_field_name("value")
                for name in _names(decl.child_by_field_name("name"), src):
                    binding = (
                        symbol(value)
                        if value is not None and value.type in _FUNCTIONS
                        else None
                    )
                    target.declare(name, binding)
                for c in decl.named_children:
                    visit(c, scope, caller)
            return
        if typ in _FUNCTIONS:
            name = _text(node.child_by_field_name("name"), src)
            binding = symbol(node)
            if typ in {"function_declaration", "generator_function_declaration"}:
                scope.declare(name, binding)
            if typ in {"arrow_function", "function_expression", "generator_function"}:
                binding = symbol(node)
            inner = Scope(scope, function=True)
            if name and typ in {"function_expression", "generator_function"}:
                inner.declare(name, binding)
            for field_name in ("parameters", "parameter"):
                for param in _names(node.child_by_field_name(field_name), src):
                    inner.declare(param)
            for c in node.named_children:
                is_body = c == node.child_by_field_name(
                    "body"
                ) or c == node.child_by_field_name("parameters")
                visit(
                    c, inner, binding.key if binding is not None and is_body else None
                )
            return
        if typ in _CLASSES:
            name = _text(node.child_by_field_name("name"), src)
            binding = symbol(node)
            if typ != "class":
                scope.declare(name, binding)
            inner = Scope(scope)
            if name:
                inner.declare(name, binding)
            for child in node.named_children:
                if child.type == "class_heritage":
                    for clause in child.named_children:
                        if clause.type == "extends_clause":
                            expr = clause.child_by_field_name("value")
                            unit.refs.append(
                                Reference(
                                    "INHERITS",
                                    binding.key if binding else None,
                                    _expression(expr, src),
                                    scope,
                                    clause.start_point[0] + 1,
                                    clause.end_point[0] + 1,
                                )
                            )
                        elif clause.type == "implements_clause":
                            unit.omissions["type_relationship"] += 1
                    # JS grammar has a direct expression, without extends_clause.
                    if child.named_children and child.named_children[0].type not in {
                        "extends_clause",
                        "implements_clause",
                    }:
                        expr = child.named_children[0]
                        unit.refs.append(
                            Reference(
                                "INHERITS",
                                binding.key if binding else None,
                                _expression(expr, src),
                                scope,
                                expr.start_point[0] + 1,
                                expr.end_point[0] + 1,
                            )
                        )
                else:
                    # Field initializers aren't calls by the enclosing function.
                    visit(child, inner, None)
            return
        if typ in {
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "function_signature",
        }:
            scope.declare(_text(node.child_by_field_name("name"), src))
            if typ == "interface_declaration" and any(
                c.type == "extends_type_clause" for c in node.named_children
            ):
                unit.omissions["type_relationship"] += 1
            return
        if typ in {
            "statement_block",
            "for_statement",
            "for_in_statement",
            "catch_clause",
            "switch_body",
            "class_static_block",
            "internal_module",
            "module",
        }:
            if typ in {"internal_module", "module"}:
                scope.declare(_text(node.child_by_field_name("name"), src))
            scope = Scope(scope)
            if typ == "for_in_statement":
                left = node.child_by_field_name("left")
                if any(c.type in {"const", "let", "var"} for c in node.children):
                    target = scope
                    if any(c.type == "var" for c in node.children):
                        while not target.function and target.parent is not None:
                            target = target.parent
                    for name in _names(left, src):
                        target.declare(name)
                else:
                    writes.append((scope, left))
            if typ == "catch_clause":
                for name in _names(node.child_by_field_name("parameter"), src):
                    scope.declare(name)
        if typ in {
            "assignment_expression",
            "augmented_assignment_expression",
            "update_expression",
        }:
            writes.append(
                (
                    scope,
                    node.child_by_field_name("left")
                    or node.child_by_field_name("argument"),
                )
            )
        if typ == "call_expression":
            expr = node.child_by_field_name("function")
            if _text(expr, src) == "eval":
                unit.dynamic_scope = True
            unit.refs.append(
                Reference(
                    "CALLS",
                    caller,
                    _expression(expr, src),
                    scope,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                )
            )
            if _text(expr, src) == "require":
                args = node.child_by_field_name("arguments")
                if (
                    args is not None
                    and args.named_children
                    and args.named_children[0].type == "string"
                ):
                    requires.append(
                        (
                            _text(args.named_children[0], src).strip("\"'"),
                            node.start_point[0] + 1,
                            scope,
                        )
                    )
        if typ == "with_statement":
            unit.dynamic_scope = True
        if typ == "new_expression":
            unit.omissions["constructor_call"] += 1
        for child in node.named_children:
            visit(child, scope, caller)

    visit(root, unit.scope, None)
    for scope, written in writes:
        # Receiver mutations and parenthesized/destructuring assignments also
        # invalidate bindings; don't mistake property keys/defaults for writes.
        def written_names(expr: Any) -> list[str]:
            if expr is None:
                return []
            if expr.type in {"member_expression", "subscript_expression"}:
                return written_names(expr.child_by_field_name("object"))
            if expr.type in {"assignment_pattern", "object_assignment_pattern"}:
                return written_names(expr.child_by_field_name("left"))
            if expr.type == "pair_pattern":
                return written_names(expr.child_by_field_name("value"))
            if expr.type in {
                "parenthesized_expression",
                "array_pattern",
                "object_pattern",
                "rest_pattern",
            }:
                return [
                    name
                    for child in expr.named_children
                    for name in written_names(child)
                ]
            return _names(expr, src)

        for name in written_names(written):
            owner = scope.owner(name)
            if owner is not None:
                owner.bindings[name] = None
    for spec, line, scope in requires:
        if scope.owner("require") is None and not unit.dynamic_scope:
            unit.imports.append((spec, line))
    return unit


def resolve(modules: list[_ParsedModule], *, calls: bool) -> list[UpsertEdge]:
    """Exact same-root path resolution, explicit ESM exports, bounded re-exports."""
    indexed = {
        (str(pm.module.key).split(":", 1)[0], str(pm.module.properties["path"])): pm
        for pm in modules
        if pm.js_units or pm.framework
    }
    edges: dict[tuple[str, str, str], UpsertEdge] = {}
    sites: dict[tuple[str, str, str], set[tuple[int, int]]] = {}

    def module_for(pm: _ParsedModule, spec: str) -> _ParsedModule | None:
        if (
            not spec.startswith(("./", "../"))
            or "\\" in spec
            or "?" in spec
            or "#" in spec
        ):
            return None
        root = str(pm.module.key).split(":", 1)[0]
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(str(pm.module.properties["path"])), spec)
        )
        if path.startswith("../") or path == "..":
            return None
        ext = posixpath.splitext(path)[1]
        if ext:
            candidates = [path]
            # TS's source extension substitution for emitted JS imports. Exact
            # source plus implementation candidates are ambiguous: don't guess.
            substitutions = {
                ".js": (".ts", ".tsx"),
                ".jsx": (".tsx",),
                ".mjs": (".mts",),
                ".cjs": (".cts",),
            }
            candidates += [path[: -len(ext)] + s for s in substitutions.get(ext, ())]
        else:
            candidates = [path + s for s in _EXTENSIONS]
            candidates += [posixpath.join(path, "index" + s) for s in _EXTENSIONS]
        matches = [indexed[root, p] for p in candidates if (root, p) in indexed]
        return matches[0] if len(matches) == 1 else None

    def imported(
        pm: _ParsedModule, binding: Binding, seen: set[tuple[str, str]]
    ) -> Binding | None:
        target = module_for(pm, binding.spec)
        if target is None or len(target.js_units) != 1 or target.framework:
            return None
        key = (str(target.module.key), binding.export)
        if key in seen or len(seen) >= 32:
            return None
        unit = target.js_units[0]
        if unit.dynamic_scope:
            return None
        value = unit.exports.get(binding.export)
        found = unit.scope.lookup(value) if isinstance(value, str) else value
        if found is not None and found.spec:
            return imported(target, found, seen | {key})
        return found

    def target_for(pm: _ParsedModule, ref: Reference) -> Binding | None:
        expr = ref.expression
        if not expr:
            return None
        found = ref.scope.lookup(expr[0])
        if len(expr) == 2:
            if found is None or found.export != "*":
                return None
            found = Binding(spec=found.spec, export=expr[1])
        if found is not None and found.spec:
            return imported(pm, found, set())
        return found

    def edge(
        pm: _ParsedModule, kind: str, caller: str, target: str, line: int, end: int
    ) -> None:
        key = kind, caller, target
        label = {"CALLS": "Function", "INHERITS": "Class", "IMPORTS": "Module"}[kind]
        edges.setdefault(
            key,
            UpsertEdge(
                type=kind,
                from_label=label,
                from_key=caller,
                to_label=label,
                to_key=target,
                source=pm.module.source,
                properties={"resolution": "js-static-v1"},
            ),
        )
        sites.setdefault(key, set()).add((line, end))

    for pm in modules:
        if not pm.js_units and not pm.framework:
            continue
        counts: Counter[str] = Counter()
        examples: list[dict[str, str | int]] = []
        for unit in pm.js_units:
            counts.update(unit.omissions)
            for spec, line in unit.imports:
                target = module_for(pm, spec)
                if target is not None:
                    edge(
                        pm,
                        "IMPORTS",
                        str(pm.module.key),
                        str(target.module.key),
                        line,
                        line,
                    )
                    counts["imports_resolved"] += 1
                else:
                    counts["imports_unresolved"] += 1
            for ref in unit.refs:
                noun = "calls" if ref.kind == "CALLS" else "bases"
                counts[noun + "_seen"] += 1
                reason = ""
                found = None
                if ref.kind == "CALLS" and not calls:
                    reason = "calls_disabled"
                elif unit.dynamic_scope:
                    reason = "dynamic_scope"
                elif ref.caller is None:
                    reason = "unindexed_caller"
                else:
                    found = target_for(pm, ref)
                    expected = "Function" if ref.kind == "CALLS" else "Class"
                    if found is None or found.label != expected:
                        reason = "unresolved_binding_or_expression"
                if not reason and found is not None and ref.caller is not None:
                    edge(pm, ref.kind, ref.caller, found.key, ref.line, ref.end)
                    counts[noun + "_resolved"] += 1
                else:
                    counts[noun + "_unresolved"] += 1
                    counts[reason] += 1
                    if len(examples) < 5:
                        examples.append(
                            {"kind": ref.kind, "line": ref.line, "reason": reason}
                        )
        pm.module.properties["code_coverage"] = json.dumps(
            {
                "mode": "partial_static",
                "calls_enabled": calls,
                "counts": dict(sorted(counts.items())),
                "examples": examples,
                "scripts": len(pm.js_units),
                "omitted": pm.script_omissions,
                "limits": "No runtime dispatch, template/cross-block calls, framework exports, wildcard exports, CommonJS symbol bindings, bundler aliases or type-only inheritance. Empty edges do not prove absence.",
            },
            separators=(",", ":"),
        )
    for key, result in edges.items():
        ordered = sorted(sites[key])
        result.properties["sites"] = json.dumps(
            {"lines": ordered[:32], "total": len(ordered)}, separators=(",", ":")
        )
    return list(edges.values())
