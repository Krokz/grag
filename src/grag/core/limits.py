"""Shared admission and work bounds. Limits fail explicitly; they never erase data.

These bound application work, not native query allocations or a model tokenizer.
Native execution also has the configured statement timeout and buffer pool.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel

from grag.core.errors import ResourceLimitError

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MUTATIONS = 1000
MAX_LABELS = 64
MAX_CANDIDATES = 1024  # per modality, shared across labels
MAX_EXPANSION_PATHS = 1024  # shared across all seeds
MAX_TOKEN_BUDGET = 32_768  # estimate, not a model-token promise
MAX_JOBS = 16  # running plus queued, per database
MAX_ACTIVE_OPERATIONS = 32
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_HISTORY_ENTRIES = 100_000
MAX_HISTORY_PER_NODE = 1000
MAX_HISTORY_BYTES = 256 * 1024 * 1024
MAX_RECEIPTS = 100_000
MAX_RECEIPT_BYTES = 64 * 1024 * 1024

# Counters compose across nested calls and labels within a read/verification.
WORK_LIMITS = {
    "result_bytes": 64 * 1024 * 1024,
    "result_rows": 100_000,
    "statements": 4096,
    "lexical_bytes": 4 * 1024 * 1024,
    "lexical_terms": 200_000,
    "packing_bytes": 128 * 1024 * 1024,
    "packing_steps": 8192,
    "source_bytes": 256 * 1024 * 1024,
    "source_document_bytes": MAX_REQUEST_BYTES,
    "source_entries": 100_000,
}


def check_size(name: str, actual: int, maximum: int, *, hint: str | None = None) -> None:
    if actual > maximum:
        raise ResourceLimitError(name, maximum, hint=hint)


def json_bytes(value: Any, maximum: int, name: str) -> int:
    """Count incrementally, stopping before constructing an oversized JSON copy."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    size = 0
    try:
        for part in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), default=str).iterencode(value):
            size += len(part.encode("utf-8"))
            check_size(name, size, maximum)
    except (RecursionError, UnicodeError) as exc:
        raise ResourceLimitError(name, maximum, hint="Use finite, shallow JSON values.") from exc
    return size


@dataclass
class WorkBudget:
    used: dict[str, int] = field(default_factory=dict)

    def charge(self, name: str, amount: int) -> None:
        self.used[name] = self.used.get(name, 0) + amount
        check_size(name, self.used[name], WORK_LIMITS[name])


_SOURCE: ContextVar[WorkBudget | None] = ContextVar("grag_source_work", default=None)

_WORK: ContextVar[WorkBudget | None] = ContextVar("grag_work", default=None)


def charge(name: str, amount: int = 1) -> None:
    work = _SOURCE.get() if name.startswith("source_") else _WORK.get()
    if work is not None:
        work.charge(name, amount)


def charge_result(row: list[Any]) -> None:
    work = _WORK.get()
    if work is not None:
        # A row has already crossed the native boundary. Use the C encoder for
        # this row (especially float vectors), then discard the transient copy.
        # Admission uses iterencode instead, before copying entire requests.
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
        work.charge("result_bytes", len(encoded.encode("utf-8")))


@contextmanager
def work_budget() -> Iterator[None]:
    if _WORK.get() is not None:
        yield
        return
    token = _WORK.set(WorkBudget())
    try:
        yield
    finally:
        _WORK.reset(token)


P = ParamSpec("P")
R = TypeVar("R")


def bounded_work(fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with work_budget():
            return fn(*args, **kwargs)
    return wrapped


def validate_request(req: BaseModel) -> None:
    json_bytes(req, MAX_REQUEST_BYTES, "request_bytes")
    if hasattr(req, "nodes") or hasattr(req, "edges"):
        check_size("mutation_items", len(getattr(req, "nodes", [])) + len(getattr(req, "edges", [])), MAX_MUTATIONS,
                   hint="Split the write into batches of at most 1000 total nodes and edges, each with its own operation_id.")
    for name, maximum in (("node_tables", 64), ("rel_tables", 64), ("documents", 256), ("paths", 64)):
        check_size(name, len(getattr(req, name, [])), maximum)


def candidate_quota(tables: list[str], requested: int) -> int:
    check_size("search_labels", len(tables), MAX_LABELS,
               hint="Specify labels to search at most 64 tables at a time.")
    return min(requested, MAX_CANDIDATES // max(1, len(tables)))


def bounded_sources(fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if _SOURCE.get() is not None:
            return fn(*args, **kwargs)
        token = _SOURCE.set(WorkBudget())
        try:
            return fn(*args, **kwargs)
        finally:
            _SOURCE.reset(token)
    return wrapped


def read_source(path: Path, max_bytes: int) -> bytes:
    work = _SOURCE.get()
    remaining = WORK_LIMITS["source_bytes"] - (work.used.get("source_bytes", 0) if work else 0)
    check_size("source_bytes", -remaining, 0)
    cap = min(max_bytes, remaining)
    with path.open("rb") as stream:
        data = stream.read(cap + 1)
    charge("source_bytes", len(data))
    check_size("source_file_bytes", len(data), cap,
               hint="Narrow the indexed paths or raise max_file_kb within its supported range. An incomplete scan cannot verify freshness.")
    return data
