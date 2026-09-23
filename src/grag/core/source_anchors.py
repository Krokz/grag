"""Content anchors for the files a memory record cites, checked again on reads.

A tracked write resolves file citations in the record's source (absolute paths, or
paths below a registered code root or its near ancestors) and stores each file's
content hash. Ordinary reads compare the current content and report cited files that
changed or went missing. Only cited files are checked: an unchanged anchor never
proves the claim is current, and changes in other files go unseen.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ANCHOR_PROP = "_source_files"  # JSON list of [cited token, absolute path, sha256]
CHANGED_PROP = "_source_changed"  # derived on reads; never stored
MAX_ANCHORS = 16
MAX_FILE_BYTES = 4 * 1024 * 1024
_ROOT_ANCESTORS = 3

_TOKEN = re.compile(r"[^\s;,()\[\]{}<>\"'`]+")
_LOCATION = re.compile(r"(?::\d+(?:[-\u2013]\d+)?)+$|#L\d+(?:-L?\d+)?$")  # hyphen or en dash ranges
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_digests: dict[tuple[str, int, int], str] = {}


def cited_paths(source: str) -> list[str]:
    """Path-like tokens in a source string, without line suffixes or URLs."""
    found: list[str] = []
    for raw in _TOKEN.findall(source):
        if "://" in raw:
            continue
        token = _LOCATION.sub("", raw.rstrip(".:"))
        if ("/" in token or _EXTENSION.search(token)) and token not in found:
            found.append(token)
    return found


def _digest(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file() or stat.st_size > MAX_FILE_BYTES:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _digests:
        if len(_digests) >= 4096:
            _digests.clear()
        try:
            _digests[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
    return _digests[key]


def search_roots(roots: list[str]) -> list[Path]:
    """Registered code roots plus a few ancestors, so checkout-relative citations resolve."""
    ordered: list[Path] = []
    for root in roots:
        path = Path(root)
        for candidate in [path, *list(path.parents)[:_ROOT_ANCESTORS]]:
            if candidate not in ordered:
                ordered.append(candidate)
    return ordered


def anchor_sources(source: str | None, roots: list[str]) -> str | None:
    """Encoded anchors for the files a source cites, or None when none resolve."""
    if not source:
        return None
    bases = search_roots(roots)
    anchors: list[list[str]] = []
    for token in cited_paths(source):
        cited = Path(token)
        for path in [cited] if cited.is_absolute() else [base / token for base in bases]:
            digest = _digest(path)
            if digest:
                anchors.append([token, str(path.resolve()), digest])
                break
        if len(anchors) >= MAX_ANCHORS:
            break
    return json.dumps(anchors, ensure_ascii=False, separators=(",", ":")) if anchors else None


def claim_changed(previous: dict | None, current: dict) -> bool:
    """True when a write creates the record or changes its claim text or source.

    Review, lifecycle and evidence-only updates, and identical re-saves, keep the
    existing anchors: re-anchoring then would clear a pending change report without
    anyone rechecking the claim.
    """
    if previous is None:
        return True

    def claim(row: dict) -> dict:
        return {k: v for k, v in row.items() if (not k.startswith("_") or k == "_source") and v is not None}

    return json.dumps(claim(previous), sort_keys=True, default=str) != json.dumps(claim(current), sort_keys=True, default=str)


def changed_sources(encoded: Any) -> list[str]:
    """Cited tokens whose file content differs from its anchor, or is missing."""
    try:
        anchors = json.loads(encoded) if isinstance(encoded, str) else []
    except ValueError:
        return []
    changed: list[str] = []
    for item in anchors if isinstance(anchors, list) else []:
        if not (isinstance(item, list) and len(item) == 3 and all(isinstance(v, str) for v in item)):
            continue
        token, path, recorded = item
        current = _digest(Path(path))
        if current is None:
            changed.append(f"{token} (missing)")
        elif current != recorded:
            changed.append(token)
    return changed
