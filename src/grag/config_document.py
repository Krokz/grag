"""Targeted MCP edits in validated JSON/JSONC, preserving unrelated text."""

from __future__ import annotations

import json
import re
from itertools import pairwise
from typing import NamedTuple


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("non-finite numbers are not valid JSON")


_DECODER = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_nonfinite)
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\r\n]*|/\*[\s\S]*?\*/|[^\s]', re.DOTALL)


def read_object(text: str, *, jsonc: bool = False) -> dict:
    value = _DECODER.decode(_masked(text, jsonc))
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


def read_server(text: str, section: str, *, jsonc: bool = False) -> dict | None:
    value = read_object(text, jsonc=jsonc)
    servers = value.get(section, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{section} must be an object")
    entry = servers.get("grag")
    if entry is not None and not isinstance(entry, dict):
        raise ValueError("grag registration must be an object")
    return entry


def _masked(text: str, jsonc: bool) -> str:
    """Replace comments/BOM/trailing commas with spaces, retaining offsets.

    Strings are tokens themselves, so URLs, escaped quotes and comment-like
    string contents are never mistaken for comments. Invalid tokens remain for
    the strict JSON decoder to reject (including unterminated block comments).
    """
    chars = list(text)
    if text.startswith("\ufeff"):
        chars[0] = " "
    if jsonc:
        tokens = list(_TOKEN.finditer(text))
        syntax = []
        for token in tokens:
            value = token.group()
            if value.startswith(("//", "/*")):
                for pos in range(token.start(), token.end()):
                    if chars[pos] not in "\r\n":
                        chars[pos] = " "
            else:
                syntax.append(token)
        for left, right in pairwise(syntax):
            if left.group() == "," and right.group() in ("}", "]"):
                chars[left.start()] = " "
    return "".join(chars)


class _Member(NamedTuple):
    key: str
    start: int
    value_start: int
    end: int


def _space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _members(mask: str, start: int) -> tuple[list[_Member], int]:
    """Read member spans from an already validated object."""
    pos = _space(mask, start + 1)
    result = []
    while mask[pos] != "}":
        key_start = pos
        key, pos = _DECODER.raw_decode(mask, pos)
        pos = _space(mask, pos) + 1  # colon
        value_start = _space(mask, pos)
        _, end = _DECODER.raw_decode(mask, value_start)
        result.append(_Member(key, key_start, value_start, end))
        pos = _space(mask, end)
        if mask[pos] == ",":
            pos = _space(mask, pos + 1)
    return result, pos


def edit_server(
    text: str, section: str, entry: dict | None, *, jsonc: bool = False
) -> str:
    """Set/remove section.grag; reject ambiguous or malformed configuration.

    Only object shapes owned by this operation are constrained. Unknown client
    settings and fields on other servers remain valid and byte-for-byte intact.
    ``None`` removes the registration. An equal entry returns the original text.
    """
    mask = _masked(text, jsonc)
    data = _DECODER.decode(mask)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")
    if section in data and not isinstance(data[section], dict):
        raise ValueError(f"{section} must be an object")
    servers = data.get(section, {})
    if "grag" in servers and not isinstance(servers["grag"], dict):
        raise ValueError(f"{section}.grag must be an object")
    if (entry is None and "grag" not in servers) or (
        entry is not None and servers.get("grag") == entry
    ):
        return text

    newline = "\r\n" if "\r\n" in text else "\n"
    root_start = _space(mask, 0)
    root, root_end = _members(mask, root_start)
    section_member = next((m for m in root if m.key == section), None)
    edits: list[tuple[int, int, str]] = []

    def indentation(pos: int) -> str:
        prefix = text[text.rfind("\n", 0, pos) + 1 : pos]
        return re.match(r"[ \t]*", prefix).group()  # type: ignore[union-attr]

    def encoded(value: dict, indent: str) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).replace(
            "\n", newline + indent
        )

    def insert(members: list[_Member], end: int, key: str, value: dict) -> None:
        # Locate a possible JSONC trailing comma without treating comments as syntax.
        if members:
            between = _masked(text[members[-1].end : end], True)
            if "," not in between:
                edits.append((members[-1].end, members[-1].end, ","))
        line_start = text.rfind("\n", 0, end) + 1
        closing_indent = text[line_start:end]
        if closing_indent.strip():
            line_start = end
            closing_indent = indentation(end)
        indent = indentation(members[0].start) if members else closing_indent + "  "
        if len(indent) <= len(closing_indent):
            indent = closing_indent + "  "
        prefix = "" if line_start and text[line_start - 1] == "\n" else newline
        content = (
            prefix + indent + json.dumps(key) + ": " + encoded(value, indent) + newline
        )
        if line_start == end:
            content += closing_indent
        edits.append((line_start, line_start, content))

    if section_member is None:
        insert(root, root_end, section, {"grag": entry})
    else:
        members, end = _members(mask, section_member.value_start)
        member = next((m for m in members if m.key == "grag"), None)
        if member is None:
            if entry is not None:
                insert(members, end, "grag", entry)
        elif entry is not None:
            edits.append(
                (
                    member.value_start,
                    member.end,
                    encoded(entry, indentation(member.start)),
                )
            )
        else:
            # Remove the property and its following comma, if present. Otherwise
            # remove the preceding comma; leave intervening comments untouched.
            tail = _masked(text[member.end : end], True)
            comma = _space(tail, 0)
            edits.append((member.start, member.end, ""))
            if comma < len(tail) and tail[comma] == ",":
                edits.append((member.end + comma, member.end + comma + 1, ""))
            elif members.index(member) > 0:
                previous = members[members.index(member) - 1]
                comma = _space(mask, previous.end)
                edits.append((comma, comma + 1, ""))

    result = text
    ordered = sorted(
        enumerate(edits), key=lambda item: (item[1][0], item[0]), reverse=True
    )
    for _, (start, end, replacement) in ordered:
        result = result[:start] + replacement + result[end:]
    # Validate the edited document and its exact semantic result before planning a write.
    expected = dict(data)
    expected[section] = dict(servers)
    if entry is None:
        del expected[section]["grag"]
    else:
        expected[section]["grag"] = entry
    if _DECODER.decode(_masked(result, jsonc)) != expected:
        raise ValueError("configuration edit did not preserve unrelated settings")
    return result
