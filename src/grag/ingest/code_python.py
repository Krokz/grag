"""Python lexical bindings for static navigation, without global-name guesses.

Unknown writes shadow known bindings for the entire scope. Calls identify source
declarations, not runtime dispatch, decorator results or monkey-patched objects.
The summaries retain neither ASTs nor source bodies.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from grag.core.types import UpsertEdge

if TYPE_CHECKING:
    from grag.ingest.code import _ParsedModule


@dataclass
class Binding:
    label: str = ""
    key: str = ""
    module: str = ""
    name: str = ""
    level: int = 0


@dataclass
class Scope:
    kind: str
    parent: Scope | None = None
    bindings: dict[str, Binding | None] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    wildcard: bool = False
    class_key: str = ""

    def declare(self, name: str, binding: Binding | None = None) -> None:
        self.bindings[name] = None if name in self.bindings else binding

    def owner(self, name: str) -> Scope | None:
        scope: Scope | None = self
        if name in self.globals:
            root = self
            while root.parent is not None:
                root = root.parent
            return root
        if name in self.nonlocals:
            scope = self.parent
            while scope is not None and scope.kind != "module":
                if name in scope.bindings:
                    return scope
                scope = scope.parent
            return None
        while scope is not None:
            if name in scope.globals:
                root = scope
                while root.parent is not None:
                    root = root.parent
                return root
            if name in scope.bindings or scope.wildcard:
                return scope
            scope = scope.parent
        return None

    def lookup(self, name: str) -> Binding | None:
        owner = self.owner(name)
        return (
            owner.bindings.get(name)
            if owner is not None and not owner.wildcard
            else None
        )


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
    path: str
    scope: Scope = field(default_factory=lambda: Scope("module"))
    classes: dict[str, Scope] = field(default_factory=dict)
    imports: list[tuple[Binding, int, int]] = field(default_factory=list)
    refs: list[Reference] = field(default_factory=list)
    omissions: Counter[str] = field(default_factory=Counter)
    dynamic: bool = False


def _expression(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = _expression(node.value)
        return (*prefix, node.attr) if prefix else ()
    return ()


def analyze(tree: ast.Module, symbols: dict[int, tuple[str, str]], path: str) -> Unit:
    unit = Unit(path)
    directed_writes: list[tuple[Scope, str]] = []
    receiver_writes: list[tuple[Scope, tuple[str, ...]]] = []
    duplicates = Counter(key for _, key in symbols.values())

    def declare(scope: Scope, name: str, binding: Binding | None = None) -> None:
        if name in scope.globals or name in scope.nonlocals:
            directed_writes.append((scope, name))
        else:
            scope.declare(name, binding)

    def directives(node: ast.AST, scope: Scope) -> None:
        # Directives apply throughout one block, including before their text.
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            if isinstance(child, ast.Global):
                scope.globals.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                scope.nonlocals.update(child.names)
            directives(child, scope)

    def visit(node: ast.AST, scope: Scope, caller: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbol = symbols.get(id(node))
            decorators = {_expression(d) for d in node.decorator_list}
            transparent = {("staticmethod",), ("classmethod",)}
            binding = Binding(*symbol) if symbol and decorators <= transparent else None
            declare(scope, node.name, binding)
            unit.omissions["decorators_not_evaluated"] += len(node.decorator_list)
            expressions: list[ast.AST] = list(node.decorator_list)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                expressions.append(node.args)
                if node.returns:
                    expressions.append(node.returns)
            unit.omissions["definition_expression_calls"] += sum(
                isinstance(item, ast.Call)
                for expr in expressions
                for item in ast.walk(expr)
            )
            # Defaults/decorators/annotations execute outside the body; do not
            # assign their calls to the newly declared function.
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    unit.refs.append(
                        Reference(
                            "inherits",
                            symbol[1] if symbol else None,
                            _expression(base),
                            scope,
                            base.lineno,
                            base.end_lineno or base.lineno,
                        )
                    )
                inner = Scope("class", scope, class_key=symbol[1] if symbol else "")
                if symbol:
                    unit.classes[symbol[1]] = inner
                new_caller = None
            else:
                parent = scope
                while parent.kind == "class" and parent.parent is not None:
                    parent = parent.parent
                inner = Scope("function", parent)
                args = node.args
                positional = [*args.posonlyargs, *args.args]
                for arg in [
                    *positional,
                    *args.kwonlyargs,
                    *([args.vararg] if args.vararg else []),
                    *([args.kwarg] if args.kwarg else []),
                ]:
                    receiver = None
                    if (
                        scope.kind == "class"
                        and positional
                        and arg is positional[0]
                        and ("staticmethod",) not in decorators
                        and decorators <= transparent
                    ):
                        receiver = Binding("receiver", scope.class_key)
                    inner.declare(arg.arg, receiver)
                new_caller = (
                    symbol[1] if symbol and duplicates[symbol[1]] == 1 else None
                )
                if symbol and duplicates[symbol[1]] > 1:
                    unit.omissions["duplicate_function_identity"] += 1
            directives(node, inner)
            for stmt in node.body:
                visit(stmt, inner, new_caller)
            return
        if isinstance(node, ast.Lambda):
            unit.omissions["lambda_body"] += 1
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            inner = Scope("comprehension", scope)
            # The first iterable is evaluated in the enclosing scope.
            for index, generator in enumerate(node.generators):
                visit(generator.iter, scope if index == 0 else inner, caller)
                visit(generator.target, inner, caller)
                for condition in generator.ifs:
                    visit(condition, inner, caller)
            for value in (
                [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            ):
                visit(value, inner, caller)
            return
        if isinstance(node, ast.NamedExpr):
            owner = scope
            while owner.kind == "comprehension" and owner.parent is not None:
                owner = owner.parent
            visit(node.target, owner, caller)
            visit(node.value, scope, caller)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = Binding(
                    module=alias.name if alias.asname else alias.name.split(".")[0]
                )
                declare(scope, alias.asname or alias.name.split(".")[0], binding)
                unit.imports.append(
                    (
                        Binding(module=alias.name),
                        node.lineno,
                        node.end_lineno or node.lineno,
                    )
                )
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                binding = Binding(
                    module=node.module or "", name=alias.name, level=node.level
                )
                unit.imports.append(
                    (binding, node.lineno, node.end_lineno or node.lineno)
                )
                if alias.name == "*":
                    scope.wildcard = True
                    unit.omissions["wildcard_import"] += 1
                else:
                    declare(scope, alias.asname or alias.name, binding)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            declare(scope, node.id)
        if isinstance(node, ast.Attribute) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            receiver_writes.append((scope, _expression(node)))
        if isinstance(node, ast.ExceptHandler) and node.name:
            declare(scope, node.name)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            declare(scope, node.name)
        if isinstance(node, ast.MatchMapping) and node.rest:
            declare(scope, node.rest)
        if isinstance(node, ast.Call):
            expr = _expression(node.func)
            if expr in {("exec",), ("eval",)}:
                unit.dynamic = True
            unit.refs.append(
                Reference(
                    "call",
                    caller,
                    expr,
                    scope,
                    node.lineno,
                    node.end_lineno or node.lineno,
                )
            )
        for child in ast.iter_child_nodes(node):
            visit(child, scope, caller)

    directives(tree, unit.scope)
    visit(tree, unit.scope, None)
    for scope, name in directed_writes:
        owner = scope.owner(name)
        if owner is not None:
            owner.bindings[name] = None
    for scope, expression in receiver_writes:
        binding = scope.lookup(expression[0]) if len(expression) == 2 else None
        if binding and binding.label == "receiver" and binding.key in unit.classes:
            unit.classes[binding.key].bindings[expression[1]] = None
    return unit


def resolve(modules: list[_ParsedModule], *, calls: bool) -> list[UpsertEdge]:
    units = {str(pm.module.key): pm for pm in modules if pm.python is not None}
    index: dict[str, list[str]] = {}
    for mid, pm in units.items():
        if pm.python is None:
            continue
        dotted = pm.python.path.removesuffix(".py").replace("/", ".")
        dotted = dotted.removesuffix(".__init__") if dotted != "__init__" else ""
        # The repository directory may itself be the importable top package.
        root = Path(str(pm.module.source))
        for _ in pm.python.path.split("/"):
            root = root.parent
        for name in {dotted, f"{root.name}.{dotted}" if dotted else root.name}:
            index.setdefault(name, []).append(mid)

    def module_for(pm: _ParsedModule, spec: str, level: int) -> _ParsedModule | None:
        if pm.python is None:
            return None
        if level:
            parts = pm.python.path.split("/")[:-1]
            if level - 1 > len(parts):
                return None
            spec = ".".join(
                [*parts[: len(parts) - level + 1], *([spec] if spec else [])]
            )
        candidates = index.get(spec, [])
        local = [
            key
            for key in candidates
            if key.split(":", 1)[0] == str(pm.module.key).split(":", 1)[0]
        ]
        # Relative imports cannot cross a root. Absolute qualified imports may
        # target another explicitly indexed root when unambiguous.
        candidates = local if local or level else candidates
        # A regular module cannot also serve as a package merely because an
        # indexed directory shares its name. Namespace packages may have no
        # __init__.py; concrete intervening modules still block that path.
        parts = spec.split(".")
        for length in range(1, len(parts)):
            parents = index.get(".".join(parts[:length]), [])
            own = [
                key
                for key in parents
                if key.split(":", 1)[0] == str(pm.module.key).split(":", 1)[0]
            ]
            parents = own if own or level else parents
            if len(parents) > 1 or any(
                parent is not None and not parent.path.endswith("__init__.py")
                for parent in (units[key].python for key in parents)
            ):
                return None
        return units[candidates[0]] if len(candidates) == 1 else None

    def follow(
        pm: _ParsedModule,
        binding: Binding | None,
        tail: tuple[str, ...],
        seen: set[tuple[str, str]],
    ) -> Binding | None:
        if binding is None or len(seen) >= 32:
            return None
        if binding.key:
            if not tail:
                return binding
            if binding.label == "receiver" and len(tail) == 1:
                if pm.python is None:
                    return None
                cls = pm.python.classes.get(binding.key)
                return cls.bindings.get(tail[0]) if cls else None
            return None
        name = binding.name
        spec = binding.module
        if not name and tail:
            spec = ".".join([spec, *tail[:-1]])
            name, tail = tail[-1], ()
        target = module_for(pm, spec, binding.level)
        if target is None and name and tail:
            # An explicitly indexed namespace-package child does not require a
            # synthetic Module node for the package directory itself.
            return follow(
                pm,
                Binding(module=f"{spec}.{name}" if spec else name, level=binding.level),
                tail,
                seen,
            )
        if (
            target is None
            or target.python is None
            or target.python.dynamic
            or target.python.scope.wildcard
        ):
            return None
        if not name:
            return Binding("Module", str(target.module.key))
        marker = (str(target.module.key), name)
        if marker in seen:
            return None
        exported = target.python.scope.bindings.get(name)
        if (
            exported is None
            and name not in target.python.scope.bindings
            and tail
            and target.python.path.endswith("__init__.py")
        ):
            # `from package import submodule; submodule.function()`.
            return follow(
                pm,
                Binding(module=f"{spec}.{name}" if spec else name, level=binding.level),
                tail,
                seen | {marker},
            )
        return follow(target, exported, tail, seen | {marker})

    result: dict[tuple[str, str, str], UpsertEdge] = {}
    sites: dict[tuple[str, str, str], set[tuple[int, int]]] = {}
    for mid, pm in units.items():
        unit = pm.python
        if unit is None:
            continue
        counts: Counter[str] = Counter()
        examples: list[dict[str, str | int]] = []

        def edge(
            kind: str,
            caller: str,
            target: Binding,
            line: int,
            end: int,
            source: str = str(pm.module.source),
        ) -> None:
            key = (kind, caller, target.key)
            if key not in result:
                label = (
                    "Function"
                    if kind == "CALLS"
                    else "Class"
                    if kind == "INHERITS"
                    else "Module"
                )
                result[key] = UpsertEdge(
                    type=kind,
                    from_label=label,
                    from_key=caller,
                    to_label=label,
                    to_key=target.key,
                    source=source,
                    properties={"resolution": "python_static_binding"},
                )
            sites.setdefault(key, set()).add((line, end))

        for binding, line, end in unit.imports:
            imported_module = module_for(pm, binding.module, binding.level)
            # Include the imported submodule when it exists, without guessing
            # that a same-named function in a different file was imported.
            if binding.name and binding.name != "*":
                sub = module_for(
                    pm,
                    ".".join(filter(None, [binding.module, binding.name])),
                    binding.level,
                )
                if sub is not None:
                    imported_module = sub
            if imported_module is not None and str(imported_module.module.key) != mid:
                edge(
                    "IMPORTS",
                    mid,
                    Binding("Module", str(imported_module.module.key)),
                    line,
                    end,
                )
                counts["imports_resolved"] += 1
            else:
                counts["imports_unresolved"] += 1
        for ref in unit.refs:
            if ref.kind == "call" and not calls:
                continue
            counts[f"{ref.kind}_sites"] += 1
            target = None
            if ref.caller and ref.expression and not unit.dynamic:
                target = follow(
                    pm, ref.scope.lookup(ref.expression[0]), ref.expression[1:], set()
                )
            expected = "Function" if ref.kind == "call" else "Class"
            if target and target.label == expected:
                edge(
                    "CALLS" if ref.kind == "call" else "INHERITS",
                    str(ref.caller),
                    target,
                    ref.line,
                    ref.end,
                )
                counts[f"{ref.kind}_resolved"] += 1
            else:
                counts[f"{ref.kind}_unresolved"] += 1
                if len(examples) < 12:
                    examples.append(
                        {
                            "kind": ref.kind,
                            "line": ref.line,
                            "expression": ".".join(ref.expression)[:160],
                            "reason": "dynamic_scope"
                            if unit.dynamic
                            else "unbound_or_unsupported",
                        }
                    )
        pm.module.properties["code_coverage"] = json.dumps(
            {
                "language": "python",
                "status": "partial",
                "calls_enabled": calls,
                "counts": dict(counts),
                "unresolved_examples": examples,
                "omissions": {
                    key: value for key, value in unit.omissions.items() if value
                },
                "limits": [
                    "runtime_dispatch",
                    "runtime_mutation",
                    "flow_sensitive_bindings",
                    "decorator_results",
                    "lambda_bodies",
                    "definition_expressions",
                    "external_packages",
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    for key, values in sites.items():
        result[key].properties["sites"] = json.dumps(
            {"lines": sorted(values)[:32], "total": len(values)}, sort_keys=True
        )
    return list(result.values())
