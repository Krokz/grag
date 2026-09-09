"""Syntax-based Java/C# overload identities and a non-destructive legacy transition."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grag.core.engine import Engine

LANGUAGES = frozenset({"java", "csharp"})
_COMMENTS = {"comment", "line_comment", "block_comment"}


def _tokens(node: Any, src: bytes) -> list[str]:
    if node is None or node.type in {
        *_COMMENTS,
        "annotation",
        "marker_annotation",
        "attribute_list",
    }:
        return []
    if not node.children:
        return [src[node.start_byte : node.end_byte].decode()]
    return [token for child in node.children for token in _tokens(child, src)]


def function_identity(node: Any, src: bytes, base: str) -> tuple[str, str, str]:
    """Exclude bodies, formatting, comments, parameter names/defaults and attributes.

    This is declaration syntax, not compiler type equivalence: aliases, generic
    substitution and erasure are not evaluated. Keep the canonical input visible
    so a consumer can inspect how an identity was distinguished.
    """
    parameters = node.child_by_field_name("parameters")
    type_parameters = node.child_by_field_name("type_parameters")
    arity = (
        len([c for c in type_parameters.named_children if c.type not in _COMMENTS])
        if type_parameters
        else 0
    )
    types: list[list[str]] = []
    if parameters is not None:
        for parameter in parameters.named_children:
            if parameter.type in _COMMENTS or parameter.type == "receiver_parameter":
                continue
            typ = parameter.child_by_field_name("type")
            if typ is not None:
                tokens = _tokens(typ, src)
            else:
                # Java varargs place the type next to a variable_declarator,
                # rather than exposing the usual `type` field.
                tokens = [
                    t
                    for c in parameter.named_children
                    if c.type
                    not in {
                        "modifiers",
                        "variable_declarator",
                        "attribute_list",
                        *_COMMENTS,
                    }
                    for t in _tokens(c, src)
                ]
            dimensions = parameter.child_by_field_name("dimensions")
            tokens += _tokens(dimensions, src)
            if parameter.type == "spread_parameter":
                tokens.append("...")
            modifiers = [
                t
                for c in parameter.children
                if c.type
                in {"modifier", "ref", "out", "in", "params", "this", "scoped"}
                for t in _tokens(c, src)
            ]
            types.append([*modifiers, *tokens])
    explicit = next(
        (c for c in node.named_children if c.type == "explicit_interface_specifier"),
        None,
    )
    constructor_static = node.type == "constructor_declaration" and any(
        "static" in _tokens(c, src)
        for c in node.named_children
        if c.type in {"modifier", "modifiers"}
    )
    identity = json.dumps(
        [
            "overload-v1",
            node.type,
            arity,
            _tokens(explicit, src),
            constructor_static,
            types,
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    body = node.child_by_field_name("body")
    if body is None:
        body = next(
            (c for c in node.named_children if c.type == "arrow_expression_clause"),
            None,
        )
    header = (
        src[node.start_byte : body.start_byte if body is not None else node.end_byte]
        .decode()
        .strip()
        .rstrip(";")
        .rstrip()
    )
    return f"{base}~{digest}", identity, header


def retire_legacy_functions(
    engine: Engine, sources: set[str], warnings: list[str]
) -> None:
    """Retain old records and authored references without guessing an overload.

    The old one-name identity may have already collapsed multiple declarations.
    Even a signature match cannot establish which overload an author intended.
    Preserve every managed legacy record (including custom properties/history),
    mark it obsolete and remove only positively owned generated edges elsewhere.
    Current replacements can be found by the old ID plus the '~' prefix.
    """
    if not sources:
        return
    rows = engine.execute_write(
        "MATCH (f:Function), (m:Module) WHERE m._source IN $sources "
        "AND f.id STARTS WITH (m.id + '#') "
        "AND (f._source_state IS NOT NULL OR f._source=m._source) "
        "AND f.language IN ['java','csharp'] AND f.identity_signature IS NULL "
        "AND (f._source_state IS NULL OR f._source_state <> 'obsolete') "
        "SET f._source_state='obsolete' RETURN count(f)",
        {"sources": sorted(sources)},
    ).rows
    if rows and rows[0][0]:
        warnings.append(
            f"Retained {rows[0][0]} legacy Java/C# Function record(s) as obsolete during overload identity migration; "
            "authored properties/history/links are preserved and are not retargeted. "
            "Find current candidates with Function.id STARTS WITH '<old-id>~', then review references explicitly."
        )
