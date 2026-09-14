"""Bounded, disposable parse summaries for one database owner.

No source bodies, syntax trees or native objects are retained. Entries never
certify graph publication or freshness: callers still read/hash selected files,
resolve the current dependency set, and compare committed graph fingerprints.
Access is serialized by Engine.code_ingest_lock.
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from grag.ingest.code import _ParsedModule


@dataclass
class _Entry:
    fingerprint: str
    parser: Callable[..., _ParsedModule]
    module: _ParsedModule
    size: int


def _retained_size(value: Any) -> int:
    """Estimate reachable Python object storage, counting cycles only once.

    Includes container/object dictionaries, not allocator overhead or RSS.
    Shared objects across entries are charged again, conservatively.
    """
    total = 0
    seen: set[int] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif hasattr(item, '__dict__'):
            pending.append(vars(item))
            if isinstance(item, BaseModel):
                pending.extend((item.__pydantic_fields_set__, item.__pydantic_extra__, item.__pydantic_private__))
    return total


class ParseCache:
    def __init__(self, *, max_bytes: int = 32 * 1024**2, max_entries: int = 2048):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.size_bytes = 0
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def _discard(self, key: tuple[str, str]) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self.size_bytes -= entry.size

    def retain_selected(self, repos: set[str], sources: set[str]) -> None:
        for key in list(self._entries):
            if key[0] in repos and key[1] not in sources:
                self._discard(key)

    def get(self, key: tuple[str, str], fingerprint: str, parser: Callable[..., _ParsedModule]) -> _ParsedModule | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint or entry.parser is not parser:
            self._discard(key)
            return None
        self._entries.move_to_end(key)
        # Resolvers add dependency-derived coverage/package metadata. Never let
        # those edits (or failed publications) contaminate the reusable parse.
        try:
            return deepcopy(entry.module)
        except RecursionError:
            self._discard(key)
            return None

    def put(self, key: tuple[str, str], fingerprint: str, parser: Callable[..., _ParsedModule], module: _ParsedModule) -> None:
        self._discard(key)
        size = _retained_size(module) + _retained_size(key) + sys.getsizeof(fingerprint) + 512
        if size > self.max_bytes or self.max_entries <= 0:
            return
        while self._entries and (self.size_bytes + size > self.max_bytes or len(self._entries) >= self.max_entries):
            self._discard(next(iter(self._entries)))
        try:
            snapshot = deepcopy(module)
        except RecursionError:
            # Deep lexical scope graphs can exceed deepcopy's recursion limit
            # even when parsing succeeds. Cache admission must stay optional.
            return
        self._entries[key] = _Entry(fingerprint, parser, snapshot, size)
        self.size_bytes += size

    def clear(self) -> None:
        self._entries.clear()
        self.size_bytes = 0
