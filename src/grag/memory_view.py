"""Bounded, schema-aware UI browsing. No required schema or inferred memories."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import SchemaError
from grag.core.evidence import HISTORY_TABLE, exclusion_reason
from grag.core.ident import validate_identifier
from grag.core.limits import MAX_LABELS, bounded_work, check_size
from grag.core.schema import build_schema_document
from grag.core.types import FreshnessReport, NodeRecord, ReadPolicy, make_node_id

SOURCE_LABELS = frozenset(
    {
        "Repo",
        "Module",
        "Class",
        "Function",
        "Constant",
        "TerraformModuleCall",
        "Document",
        "Section",
        "Chunk",
    }
)
OPEN_STATUSES = (
    "open",
    "todo",
    "pending",
    "ready",
    "active",
    "in_progress",
    "in-progress",
    "blocked",
)


class MemoryListRequest(ReadPolicy):
    label: str | None = None
    query: str = Field(default="", max_length=256)
    view: Literal["browse", "tasks", "recent"] = "browse"
    include_inactive: bool = False
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=30, ge=1, le=100)


class MemoryItem(BaseModel):
    id: str
    label: str
    key: Any
    title: str
    preview: str
    status: str | None = None
    source: str | None = None
    changed_at: str | None = None
    change_kind: Literal["recorded", "created", "unknown"] = "unknown"
    evidence_state: str | None = None
    review: str | None = None
    excluded_reason: str | None = None
    tracked: bool = False
    managed: bool = False


class MemoryList(BaseModel):
    items: list[MemoryItem]
    total: int
    offset: int
    next_offset: int | None
    freshness: FreshnessReport


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        return (
            parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)
            .astimezone(dt.timezone.utc)
            .isoformat()
        )
    except ValueError:
        return None


@bounded_work
def list_memories(
    engine: Engine, config: GragConfig, req: MemoryListRequest, report: FreshnessReport
) -> MemoryList:
    # Keep the catalogue, evidence metadata and node summaries in one state.
    # Shared row/byte/statement limits stop oversized scans explicitly.
    with engine.serialized_writes():
        schema = build_schema_document(engine, config, detail="compact")
        tables = {t.name: t for t in schema.node_tables}
        if req.label and req.label not in tables:
            raise SchemaError(f"Unknown node label {req.label!r}.")
        selected = (
            [req.label]
            if req.label
            else [name for name in tables if name not in SOURCE_LABELS]
        )
        if req.view == "tasks":
            selected = [name for name in selected if name.casefold() == "task"]
        check_size(
            "memory_labels", len(selected), MAX_LABELS, hint="Choose one memory type."
        )
        items: dict[str, MemoryItem] = {}
        sequences: dict[str, int] = {}
        now = dt.datetime.now(dt.timezone.utc)
        for name in selected:
            validate_identifier(name, "node label")
            columns = {p.name: p.type for p in tables[name].properties}
            pk = next(
                (p.name for p in tables[name].properties if p.is_primary_key), None
            )
            if not pk:
                raise SchemaError(f"No primary key on {name}.")
            for prop in columns:
                validate_identifier(prop, "property")
            text_columns = [
                p
                for p, kind in columns.items()
                if kind == "STRING" and not p.startswith("_") and p != "code_coverage"
            ]
            title = [p for p in ("title", "name", "heading") if p in text_columns]
            prose = [
                p
                for p in (
                    "body",
                    "summary",
                    "text",
                    "description",
                    "content",
                    "rationale",
                )
                if p in text_columns
            ]

            def summary(props: Sequence[str], fallback: str, length: int) -> str:
                args = ", ".join([*(f"n.{p}" for p in props), fallback])
                return f"substring(coalesce({args}), 1, {length})"

            projection = [
                f"n.{pk} AS key",
                f"{summary(title, f'CAST(n.{pk} AS STRING)', 160)} AS title",
                f"{summary(prose, chr(39) + chr(39), 320)} AS preview",
            ]
            metadata = [
                p
                for p in (
                    "status",
                    "_source",
                    "_created_at",
                    "_evidence_state",
                    "_review_state",
                    "_expires_at",
                    "_evidence_seq",
                    "_document_state",
                    "_source_state",
                )
                if p in columns
            ]
            projection += [f"n.{p} AS {p}" for p in metadata]
            managed = [
                f"n.{p} IS NOT NULL"
                for p in (
                    "_document_owner",
                    "_document_state",
                    "_document_identity",
                    "_document_file",
                    "_ingest_hash",
                    "_source_state",
                )
                if p in columns
            ]
            projection.append(f"({' OR '.join(managed) or 'false'}) AS managed")
            conditions = []
            if req.query:
                # Match full stored text, not only the list excerpt. Values stay parameters.
                conditions.append(
                    "("
                    + " OR ".join(
                        f"lower(coalesce(n.{p},'')) CONTAINS $query"
                        for p in text_columns
                    )
                    + ")"
                    if text_columns
                    else "false"
                )
            if req.view == "tasks":
                conditions.append(
                    "lower(trim(coalesce(n.status,''))) IN $statuses"
                    if columns.get("status") == "STRING"
                    else "false"
                )
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            params: dict[str, Any] = {"query": req.query.lower()} if req.query else {}
            if req.view == "tasks" and columns.get("status") == "STRING":
                params["statuses"] = list(OPEN_STATUSES)
            rows = engine.execute(
                f"MATCH (n:{name}){where} RETURN {', '.join(projection)}", params
            ).as_dicts()
            for row in rows:
                if row["managed"] and not req.label:
                    continue
                identity = make_node_id(name, row["key"])
                excluded = exclusion_reason(
                    NodeRecord(id=identity, label=name, properties=row), now
                )
                if excluded and not req.include_inactive:
                    continue
                created = _timestamp(row.get("_created_at"))
                items[identity] = MemoryItem(
                    id=identity,
                    label=name,
                    key=row["key"],
                    title=row["title"],
                    preview=row["preview"],
                    status=str(row["status"])
                    if row.get("status") is not None
                    else None,
                    source=str(row["_source"])[:1024] if row.get("_source") else None,
                    changed_at=created,
                    change_kind="created" if created else "unknown",
                    evidence_state=row.get("_evidence_state"),
                    review=row.get("_review_state"),
                    excluded_reason=excluded,
                    tracked=row.get("_evidence_seq") is not None,
                    managed=row["managed"],
                )
                if row.get("_evidence_seq") is not None:
                    sequences[identity] = row["_evidence_seq"]
        if req.view == "recent" and sequences:
            # Read metadata only, never revision bodies. History is opt-in and
            # may not cover raw writes or ingestion; creation time is labelled separately.
            for name in selected:
                history_rows = engine.execute(
                    f"MATCH (h:{HISTORY_TABLE}) WHERE h.node_id STARTS WITH $prefix RETURN h.node_id, h.sequence, h.metadata",
                    {"prefix": name + ":"},
                ).rows
                for identity, sequence, metadata_json in history_rows:
                    if sequences.get(identity) != sequence:
                        continue
                    stamp = _timestamp(json.loads(metadata_json).get("recorded_at"))
                    if stamp:
                        items[identity].changed_at = stamp
                        items[identity].change_kind = "recorded"
        result = sorted(
            items.values(), key=lambda item: (item.title.casefold(), item.id)
        )
        if req.view == "recent":
            result.sort(key=lambda item: item.changed_at or "", reverse=True)
        end = req.offset + req.limit
        return MemoryList(
            items=result[req.offset : end],
            total=len(result),
            offset=req.offset,
            next_offset=end if end < len(result) else None,
            freshness=report,
        )
