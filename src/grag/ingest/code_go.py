"""Conservative Go package navigation, with explicit limits instead of guessed targets.

Tree summaries retain declarations and references, not trees or function bodies.
Resolution uses package directory/name AND repo identity; imports need go.mod paths.
Basic interface method sets are compared structurally without running a compiler.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from grag.core.types import UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from grag.ingest.code import _ParsedModule


@dataclass
class Function:
    key: str
    name: str
    line: int
    end: int
    receiver: str = ""
    pointer: bool = False
    signature: Any = None
    bindings: dict[str, Any] = field(default_factory=dict)
    refs: list[tuple[Any, int, int]] = field(default_factory=list)
    omitted_closures: int = 0


@dataclass
class Type:
    key: str
    name: str
    kind: str
    line: int
    end: int
    safe: bool
    methods: list[Function] = field(default_factory=list)
    fields: set[str] = field(default_factory=set)


@dataclass
class Unit:
    package: str
    import_path: str | None = None
    imports: list[tuple[str, str, int, int]] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    types: list[Type] = field(default_factory=list)
    globals: set[str] = field(default_factory=set)
    build_constraints: bool = False


def _text(node: Any) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _field(node: Any, name: str) -> Any:
    return node.child_by_field_name(name) if node is not None else None


def _children(node: Any) -> list[Any]:
    return [child for child in node.named_children if child.type != "comment"]


def _names(node: Any) -> list[str]:
    return [
        _text(child)
        for i, child in enumerate(node.children)
        if node.field_name_for_child(i) == "name" and child.is_named
    ]


def _type(node: Any) -> Any:
    if node is None:
        return None
    if node.type == "type_identifier":
        return ("name", _text(node))
    if node.type == "qualified_type":
        return (
            "qualified",
            _text(_field(node, "package")),
            _text(_field(node, "name")),
        )
    if node.type in {"pointer_type", "slice_type"}:
        return (node.type, _type(_children(node)[-1]))
    if node.type == "map_type":
        return ("map", _type(_field(node, "key")), _type(_field(node, "value")))
    # Generics, aliases, anonymous types, function types and array-length
    # expressions require more type analysis. Do not compare their source strings.
    return None


def _parameters(node: Any) -> tuple | None:
    if node is None:
        return ()
    if node.type != "parameter_list":
        return (_type(node),)
    result = []
    for child in node.named_children:
        if child.type == "comment":
            continue
        kind = _type(_field(child, "type"))
        if child.type == "variadic_parameter_declaration":
            kind = ("variadic", kind)
        result.extend([kind] * max(1, len(_names(child))))
    return tuple(result)


def _receiver(node: Any) -> tuple[str, bool]:
    params = _field(node, "receiver")
    if params is None or not _children(params):
        return "", False
    kind = _field(_children(params)[0], "type")
    pointer = kind is not None and kind.type == "pointer_type"
    while kind is not None and kind.type in {"pointer_type", "generic_type"}:
        kind = _children(kind)[0]
    return _text(kind), pointer


def _expression(node: Any) -> Any:
    if node is None:
        return None
    if node.type in {"identifier", "type_identifier"}:
        return ("name", _text(node))
    if node.type == "selector_expression":
        return (
            "selector",
            _expression(_field(node, "operand")),
            _text(_field(node, "field")),
        )
    if node.type == "parenthesized_expression" and _children(node):
        return _expression(_children(node)[0])
    if node.type == "unary_expression" and _text(node).lstrip().startswith("*"):
        return ("deref", _expression(_field(node, "operand")))
    return None


def _literal_type(node: Any) -> Any:
    if node is None:
        return None
    if node.type == "composite_literal":
        return _type(_field(node, "type"))
    if node.type == "unary_expression" and _text(node).lstrip().startswith("&"):
        return ("pointer_type", _literal_type(_field(node, "operand")))
    return None


def _collect_function(node: Any, key: str) -> Function:
    receiver, pointer = _receiver(node)
    fn = Function(
        key,
        _text(_field(node, "name")),
        node.start_point[0] + 1,
        node.end_point[0] + 1,
        receiver,
        pointer,
        (_parameters(_field(node, "parameters")), _parameters(_field(node, "result"))),
    )
    declared: Counter = Counter()

    def bind(name: str, kind: Any) -> None:
        if name == "_":
            return
        declared[name] += 1
        fn.bindings[name] = kind if declared[name] == 1 else None

    for field_name in ("receiver", "parameters", "result", "type_parameters"):
        params = _field(node, field_name)
        if params is not None:
            for child in params.named_children:
                kind = _type(_field(child, "type"))
                if child.type == "variadic_parameter_declaration":
                    kind = ("slice_type", kind)
                for name in _names(child):
                    bind(name, kind)

    def visit(current: Any) -> None:
        if current.type == "func_literal":
            fn.omitted_closures += 1
            return  # No indexed caller for this body; never attribute it to its parent.
        if current.type in {"var_spec", "const_spec", "type_spec", "type_alias"}:
            for name in _names(current):
                bind(
                    name,
                    _type(_field(current, "type"))
                    if current.type == "var_spec"
                    else None,
                )
        elif current.type == "short_var_declaration":
            left, right = _field(current, "left"), _field(current, "right")
            values = _children(right) if right is not None else []
            if left is not None:
                for i, child in enumerate(_children(left)):
                    if child.type == "identifier":
                        bind(
                            _text(child),
                            _literal_type(values[i])
                            if len(values) == len(_children(left))
                            else None,
                        )
        elif current.type in {
            "range_clause",
            "receive_statement",
            "type_switch_statement",
        } and ":=" in _text(current):
            # Shadow these declarations throughout the function conservatively.
            left = _field(current, "left") or _field(current, "alias")
            if left is not None:
                for child in (
                    [left] if left.type == "identifier" else left.named_children
                ):
                    if child.type == "identifier":
                        bind(_text(child), None)
        if current.type == "call_expression":
            fn.refs.append(
                (
                    _expression(_field(current, "function")),
                    current.start_point[0] + 1,
                    current.end_point[0] + 1,
                )
            )
        for child in current.named_children:
            visit(child)

    body = _field(node, "body")
    if body is not None:
        visit(body)
    return fn


def parse(root: Any, src: bytes, pm: _ParsedModule, rel_path: str) -> Unit:
    from grag.ingest.code_ts import _add_class, _add_function, _docstring

    package = next(
        (
            _text(_children(n)[0])
            for n in root.named_children
            if n.type == "package_clause"
        ),
        "",
    )
    unit = Unit(package, build_constraints=b"//go:build" in src or b"// +build" in src)
    mid, source = str(pm.module.key), str(pm.module.source)
    for node in root.named_children:
        if node.type == "import_declaration":
            stack = list(node.named_children)
            while stack:
                spec = stack.pop(0)
                if spec.type != "import_spec":
                    stack.extend(spec.named_children)
                    continue
                raw = _text(_field(spec, "path"))
                try:
                    path = (
                        raw[1:-1].replace("\r", "")
                        if raw.startswith("`")
                        else json.loads(raw)
                    )
                except ValueError:
                    unit.imports.append(
                        (
                            _text(_field(spec, "name")),
                            "",
                            spec.start_point[0] + 1,
                            spec.end_point[0] + 1,
                        )
                    )
                    continue
                unit.imports.append(
                    (
                        _text(_field(spec, "name")),
                        path,
                        spec.start_point[0] + 1,
                        spec.end_point[0] + 1,
                    )
                )
        elif node.type == "type_declaration":
            specs = [
                n for n in node.named_children if n.type in {"type_spec", "type_alias"}
            ]
            for spec in specs:
                name, kind = _text(_field(spec, "name")), _field(spec, "type")
                if not name or kind is None:
                    continue
                go_kind = (
                    "alias"
                    if spec.type == "type_alias"
                    else "named"
                    if kind.type == "type_identifier"
                    else kind.type.removesuffix("_type")
                )
                _add_class(
                    pm,
                    spec,
                    node if len(specs) == 1 else spec,
                    src,
                    name=name,
                    qual=name,
                    rel_path=rel_path,
                    language="go",
                    source_path=source,
                )
                pm.classes[-1].properties["kind"] = go_kind
                fields = (
                    [
                        f
                        for group in kind.named_children
                        for f in group.named_children
                        if f.type == "field_declaration"
                    ]
                    if kind.type == "struct_type"
                    else []
                )
                safe = (
                    spec.type != "type_alias"
                    and _field(spec, "type_parameters") is None
                )
                safe = safe and kind.type not in {"generic_type", "pointer_type"}
                if kind.type == "type_identifier" and _text(kind) not in _BUILTINS - {
                    "error"
                }:
                    safe = False
                safe = safe and all(_names(f) for f in fields)
                safe = safe and (
                    kind.type != "interface_type"
                    or all(
                        n.type in {"method_elem", "comment"}
                        for n in kind.named_children
                    )
                )
                item = Type(
                    f"{mid}#{name}",
                    name,
                    go_kind,
                    spec.start_point[0] + 1,
                    spec.end_point[0] + 1,
                    safe,
                    fields={name for f in fields for name in _names(f)},
                )
                unit.types.append(item)
                if kind.type == "interface_type":
                    for method in kind.named_children:
                        if method.type != "method_elem":
                            continue
                        mname = _text(_field(method, "name"))
                        fid = _add_function(
                            pm,
                            method,
                            method,
                            src,
                            name=mname,
                            qual=f"{name}.{mname}",
                            parent_class=name,
                            rel_path=rel_path,
                            language="go",
                            source_path=source,
                        )
                        item.methods.append(_collect_function(method, fid))
        elif node.type in {"function_declaration", "method_declaration"}:
            name = _text(_field(node, "name"))
            receiver, _ = _receiver(node)
            qual = (
                f"{receiver}.{name}"
                if receiver
                else f"init@{node.start_point[0] + 1}"
                if name == "init"
                else name
            )
            fid = _add_function(
                pm,
                node,
                node,
                src,
                name=name,
                qual=qual,
                parent_class=receiver or None,
                rel_path=rel_path,
                language="go",
                source_path=source,
                link_to_class=False,
            )
            unit.functions.append(_collect_function(node, fid))
        elif node.type in {"const_declaration", "var_declaration"}:
            previous: Any = None
            previous_type = ""
            previous_line = 0
            for index, spec in enumerate(
                n for n in node.named_children if n.type in {"const_spec", "var_spec"}
            ):
                names = _names(spec)
                unit.globals.update(names)
                if node.type != "const_declaration":
                    continue
                value = _field(spec, "value")
                if value is not None:
                    previous = _children(value)
                    previous_type = _text(_field(spec, "type"))
                    previous_line = spec.start_point[0] + 1
                for i, name in enumerate(names):
                    if name == "_":
                        continue
                    expression = (
                        _text(previous[i])
                        if previous is not None and i < len(previous)
                        else ""
                    )
                    key = f"{mid}#{name}"
                    pm.constants.append(
                        UpsertNode(
                            label="Constant",
                            key=key,
                            source=source,
                            properties={
                                "name": name,
                                "path": rel_path,
                                "language": "go",
                                "line_start": spec.start_point[0] + 1,
                                "line_end": spec.end_point[0] + 1,
                                "expression": expression,
                                "declared_type": previous_type,
                                "expression_line": previous_line,
                                "iota_index": index,
                                "inherited_expression": value is None,
                                "docstring": _docstring(spec, src)
                                or _docstring(node, src),
                            },
                        )
                    )
                    pm.contains.append(("CONTAINS_MODULE_CONSTANT", mid, key))
    return unit


_BUILTINS = {
    "bool",
    "string",
    "int",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uintptr",
    "float32",
    "float64",
    "complex64",
    "complex128",
    "error",
    "byte",
    "rune",
}


def resolve(modules: list[_ParsedModule], *, calls: bool) -> list[UpsertEdge]:
    units = [pm for pm in modules if pm.go is not None]
    groups: dict[tuple, list[Any]] = defaultdict(list)
    paths: dict[str, set[tuple]] = defaultdict(set)
    memberships: dict[str, tuple] = {}
    for pm in units:
        unit = cast(Unit, pm.go)
        group_key = (
            str(pm.module.key).split(":", 1)[0],
            posixpath.dirname(pm.module.properties["path"]),
            unit.package,
        )
        memberships[str(pm.module.key)] = group_key
        groups[group_key].append(pm)
        external_test = unit.package.endswith("_test") and str(
            pm.module.properties["path"]
        ).endswith("_test.go")
        if unit.import_path and not external_test:
            paths[unit.import_path].add(group_key)
    types: dict[tuple, list[tuple[Any, Type]]] = defaultdict(list)
    funcs: dict[tuple, list[tuple[Any, Function]]] = defaultdict(list)
    methods: dict[tuple, list[tuple[Any, Function]]] = defaultdict(list)
    globals_: dict[tuple, set[str]] = defaultdict(set)
    imports: dict[str, dict[str, tuple | None]] = {}
    for pm in units:
        unit, group = cast(Unit, pm.go), memberships[str(pm.module.key)]
        globals_[group].update(unit.globals)
        for item in unit.types:
            types[(*group, item.name)].append((pm, item))
            for method in item.methods:
                methods[(*group, item.name, method.name)].append((pm, method))
        for fn in unit.functions:
            (methods if fn.receiver else funcs)[
                (*group, fn.receiver, fn.name) if fn.receiver else (*group, fn.name)
            ].append((pm, fn))
        bound: dict[str, tuple | None] = {}
        for alias, path, _, _ in unit.imports:
            import_candidates = paths.get(path, set())
            target_group = (
                next(iter(import_candidates)) if len(import_candidates) == 1 else None
            )
            name = alias or (
                target_group[-1] if target_group else path.rsplit("/", 1)[-1]
            )
            bound[name] = target_group if name not in bound else None
        imports[str(pm.module.key)] = bound

    def type_key(pm: Any, ref: Any) -> tuple | None:
        if not ref:
            return None
        group = memberships[str(pm.module.key)]
        if ref[0] == "name":
            key = (*group, ref[1])
        elif ref[0] == "qualified":
            target = imports[str(pm.module.key)].get(ref[1])
            if target is None or not ref[2][:1].isupper():
                return None
            key = (*target, ref[2])
        else:
            return None
        found = types.get(key, [])
        return key if len(found) == 1 and found[0][1].kind != "alias" else None

    def canonical(pm: Any, ref: Any) -> Any:
        if not ref:
            return None
        if ref[0] == "name":
            # Package declarations shadow predeclared types too.
            key = type_key(pm, ref)
            if key:
                return key
            group = memberships[str(pm.module.key)]
            if (*group, ref[1]) not in types and ref[1] in _BUILTINS:
                return {"byte": "uint8", "rune": "int32"}.get(ref[1], ref[1])
            return None
        if ref[0] == "qualified":
            return type_key(pm, ref)
        nested = tuple(canonical(pm, child) for child in ref[1:])
        return (ref[0], *nested) if all(child is not None for child in nested) else None

    def signature(pm: Any, fn: Function) -> Any:
        sides = tuple(tuple(canonical(pm, t) for t in side) for side in fn.signature)
        return sides if all(t is not None for side in sides for t in side) else None

    interface_index: dict[str, list[tuple[tuple, Any, Type]]] = defaultdict(list)
    method_names: dict[tuple, set[str]] = defaultdict(set)
    for method_key in methods:
        method_names[method_key[:-1]].add(method_key[-1])
    for interface_key, entries in types.items():
        if len(entries) != 1:
            continue
        owner, interface = entries[0]
        if interface.kind == "interface" and interface.safe and interface.methods:
            interface_index[interface.methods[0].name].append(
                (interface_key, owner, interface)
            )

    edges: dict[tuple, UpsertEdge] = {}
    sites: dict[tuple, set[tuple[int, int]]] = defaultdict(set)

    def edge(
        pm: Any,
        rel: str,
        left: str,
        right: str,
        resolution: str,
        line: int,
        end: int,
        **properties: Any,
    ) -> None:
        label1, label2 = {
            "CALLS": ("Function", "Function"),
            "IMPORTS": ("Module", "Module"),
            "CONTAINS_CLASS_FUNCTION": ("Class", "Function"),
            "IMPLEMENTS_INTERFACE": ("Class", "Class"),
        }[rel]
        key = (rel, left, right)
        edges[key] = UpsertEdge(
            type=rel,
            from_label=label1,
            from_key=left,
            to_label=label2,
            to_key=right,
            source=str(pm.module.source),
            properties={"resolution": resolution, **properties}
            if rel != "CONTAINS_CLASS_FUNCTION"
            else {},
        )
        sites[key].add((line, end))

    for pm in units:
        unit, mid = cast(Unit, pm.go), str(pm.module.key)
        group = memberships[mid]
        counts: Counter = Counter()
        examples: list[dict[str, Any]] = []

        def missed(
            kind: str,
            line: int,
            reason: str,
            *,
            counts: Counter = counts,
            examples: list = examples,
        ) -> None:
            counts[kind + "_unresolved"] += 1
            if len(examples) < 8:
                examples.append({"kind": kind, "line": line, "reason": reason})

        for alias, path, line, end in unit.imports:
            import_candidates = paths.get(path, set())
            if len(import_candidates) != 1 or alias == ".":
                missed(
                    "imports",
                    line,
                    "dot import"
                    if alias == "."
                    else "external, missing go.mod path, or ambiguous package",
                )
                continue
            for target_pm in groups[next(iter(import_candidates))]:
                if target_pm.module.key != mid:
                    edge(
                        pm,
                        "IMPORTS",
                        mid,
                        str(target_pm.module.key),
                        "go_module_path",
                        line,
                        end,
                    )
            counts["imports_resolved"] += 1
        for fn in unit.functions:
            if fn.receiver:
                key = type_key(pm, ("name", fn.receiver))
                if key:
                    edge(
                        pm,
                        "CONTAINS_CLASS_FUNCTION",
                        types[key][0][1].key,
                        fn.key,
                        "",
                        fn.line,
                        fn.end,
                    )
            counts["unindexed_closures"] += fn.omitted_closures
            if not calls:
                continue
            for expr, line, end in fn.refs:
                target: tuple[Any, Function] | None = None
                resolution = "go_package_function"
                if any(a == "." for a, _, _, _ in unit.imports):
                    missed(
                        "calls", line, "dot import prevents reliable lexical binding"
                    )
                    continue
                if expr and expr[0] == "name":
                    name = expr[1]
                    found = funcs.get((*group, name), [])
                    if (
                        name not in fn.bindings
                        and name not in imports[mid]
                        and (*group, name) not in types
                        and name not in globals_[group]
                        and len(found) == 1
                        and name != "init"
                        and not any(a == "." for a, _, _, _ in unit.imports)
                    ):
                        target = found[0]
                elif expr and expr[0] == "selector":
                    operand, name = expr[1:]
                    if operand and operand[0] == "name":
                        variable = operand[1]
                        imported = (
                            imports[mid].get(variable)
                            if variable not in fn.bindings
                            and variable not in globals_[group]
                            else None
                        )
                        if imported and name[:1].isupper():
                            found = funcs.get((*imported, name), [])
                            target = found[0] if len(found) == 1 else None
                            resolution = "go_imported_function"
                        else:
                            ref: Any = (
                                fn.bindings.get(variable)
                                if variable in fn.bindings
                                else ("name", variable)
                                if variable not in globals_[group]
                                else None
                            )
                            # Local type/identifier shadowing must not bind to a
                            # package declaration with the same spelling.
                            base_ref = ref
                            while base_ref and base_ref[0] == "pointer_type":
                                base_ref = base_ref[1]
                            if (
                                base_ref
                                and base_ref[0] in {"name", "qualified"}
                                and base_ref[1] in fn.bindings
                            ):
                                ref = None
                            pointer = bool(ref and ref[0] == "pointer_type")
                            key = type_key(pm, ref[1] if pointer else ref)
                            found = methods.get((*key, name), []) if key else []
                            if (
                                key
                                and len(found) == 1
                                and types[key][0][1].safe
                                and name not in types[key][0][1].fields
                            ):
                                kind = types[key][0][1].kind
                                candidate = found[0]
                                # Variables with a known concrete value type are addressable;
                                # T.Method expressions cannot select pointer-only methods.
                                if (
                                    (
                                        not candidate[1].pointer
                                        or pointer
                                        or variable in fn.bindings
                                    )
                                    and (not pointer or kind != "interface")
                                    and (key[:3] == group or name[:1].isupper())
                                ):
                                    target = candidate
                                    resolution = (
                                        "go_interface_method"
                                        if kind == "interface"
                                        else "go_static_receiver"
                                    )
                if target:
                    edge(pm, "CALLS", fn.key, target[1].key, resolution, line, end)
                    counts["calls_resolved"] += 1
                else:
                    missed(
                        "calls",
                        line,
                        "dynamic, shadowed, unindexed, or unsupported target",
                    )
        # Only basic, nonempty interface method sets. Empty interfaces would
        # generate an unhelpful all-types graph; embedded/generic sets need typing.
        for concrete in unit.types:
            key = (*group, concrete.name)
            if (
                concrete.kind in {"interface", "alias"}
                or not concrete.safe
                or len(types[key]) != 1
            ):
                continue
            matching_interfaces = [
                entry
                for name in sorted(method_names.get(key, set()))
                for entry in interface_index.get(name, [])
            ]
            for ikey, ipm, interface in matching_interfaces:
                pointer_only = False
                valid = True
                for required in interface.methods:
                    implementations = methods.get(
                        (*group, concrete.name, required.name), []
                    )
                    if len(implementations) != 1 or (
                        not required.name[:1].isupper() and ikey[:3] != group
                    ):
                        valid = False
                        break
                    owner, implementation = implementations[0]
                    wanted, actual = (
                        signature(ipm, required),
                        signature(owner, implementation),
                    )
                    if (
                        wanted is None
                        or wanted != actual
                        or required.name in concrete.fields
                    ):
                        valid = False
                        break
                    pointer_only |= implementation.pointer
                if valid:
                    edge(
                        pm,
                        "IMPLEMENTS_INTERFACE",
                        concrete.key,
                        interface.key,
                        "go_basic_method_set",
                        concrete.line,
                        concrete.end,
                        method_set="pointer" if pointer_only else "value_and_pointer",
                    )
                    counts["interfaces_resolved"] += 1
        counts["constants"] = len(pm.constants)
        counts["types_unsupported"] = sum(not t.safe for t in unit.types)
        pm.module.properties["code_coverage"] = json.dumps(
            {
                "language": "go",
                "mode": "partial_static",
                "calls_enabled": calls,
                "package": unit.package,
                "import_path": unit.import_path,
                "counts": dict(sorted(counts.items())),
                "examples": examples,
                "build_constraints_present": unit.build_constraints,
                "limits": "Selected-file union, not a Go build/type check. No build-tag selection, go.work/replace/vendor resolution, dynamic dispatch, function-variable calls, closures, promoted/embedded methods, generic/alias method sets or empty-interface edges. Interface CALLS target the declaration, not concrete implementations. Constant expressions are source text, not evaluated values. Empty edges do not prove absence.",
            },
            separators=(",", ":"),
        )
    for key, result in edges.items():
        if result.type != "CONTAINS_CLASS_FUNCTION":
            ordered = sorted(sites[key])
            result.properties["sites"] = json.dumps(
                {"lines": ordered[:32], "total": len(ordered)}, separators=(",", ":")
            )
    return list(edges.values())
