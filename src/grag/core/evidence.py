"""Explicit memory lifecycle and opt-in, transactional authored revision history.

No preset schema or background expiry worker. Untracked graphs keep working;
expiry is evaluated at read time and never deletes evidence. History begins at
adoption, with a baseline for an existing node, not invented earlier revisions.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from grag.core.engine import Engine, node_record_from_value
from grag.core.errors import ConfigurationError, NotFoundError, SchemaError
from grag.core.limits import (
    MAX_HISTORY_BYTES,
    MAX_HISTORY_ENTRIES,
    MAX_HISTORY_PER_NODE,
    MAX_SNAPSHOT_BYTES,
    check_size,
    json_bytes,
)
from grag.core.mutate import _node_pks, _table_columns, _table_index
from grag.core.revisions import canonical_json, content_revision
from grag.core.types import (
    VECTOR_PROPS,
    ContextRequest,
    ContextResponse,
    EvidenceHistory,
    EvidenceHistoryEntry,
    FreshnessReport,
    NodeRecord,
    UpsertNode,
    make_node_id,
    split_node_id,
)

HISTORY_TABLE = "_grag_evidence_history"
COLUMNS = {
    "_evidence_state": "STRING", "_review_state": "STRING",
    "_expires_at": "STRING", "_superseded_by": "STRING", "_evidence_seq": "INT64",
}
# These cannot be dropped while retaining prose that they qualify.
SAFETY_PROPS = frozenset({*COLUMNS, "_document_state", "_history_revision", "_evidence_visibility", "status"})


def exclusion_reason(node: NodeRecord, now: dt.datetime) -> str | None:
    props = node.properties
    if props.get("_document_state") == "obsolete":
        return "obsolete_document"
    # The small legacy convention is deliberate: task open/done/blocked and
    # arbitrary business statuses are not lifecycle filters.
    state = props.get("_evidence_state") or props.get("status")
    if isinstance(state, str) and state.strip().casefold() in {"superseded", "retracted", "expired"}:
        return state.strip().casefold()
    if props.get("_review_state") == "disputed":
        return "disputed"
    expiry = props.get("_expires_at")
    if expiry:
        try:
            parsed = dt.datetime.fromisoformat(expiry)
            if parsed.utcoffset() is None:
                return "invalid_expiry"
            if parsed <= now:
                return "expired"
        except (ValueError, TypeError):
            return "invalid_expiry"
    return None


def _snapshot(value: dict) -> dict:
    return {k: v for k, v in value.items() if k not in VECTOR_PROPS and k != "_ID" and v is not None}


def _ensure(engine: Engine, label: str) -> None:
    columns = _table_columns(engine, label)
    for name, kind in {**COLUMNS, "_source": "STRING", "_created_at": "TIMESTAMP"}.items():
        if name not in columns:
            # Untyped DEFAULT NULL writes an ANY-typed expression into the
            # 0.20.2 WAL and can make a committed ALTER fail strict replay.
            engine.execute_write(f"ALTER TABLE {label} ADD {name} {kind} DEFAULT CAST(NULL AS {kind})")
    if HISTORY_TABLE not in _table_index(engine):
        engine.execute_write(
            f"CREATE NODE TABLE {HISTORY_TABLE}(id STRING PRIMARY KEY, node_id STRING, "
            "sequence INT64, snapshot STRING, metadata STRING)"
        )


def before_update(engine: Engine, node: UpsertNode, pk: str) -> dict | None:
    """Validate adoption/patch and prepare DDL inside the caller's transaction."""
    rows = engine.execute(f"MATCH (n:{node.label} {{{pk}: $key}}) RETURN n", {"key": node.key}).rows
    previous = rows[0][0] if rows else None
    tracked = previous is not None and previous.get("_evidence_seq") is not None
    if node.evidence is None and not tracked:
        return None
    if node.evidence is not None and previous is not None and node.expected_revision is None:
        raise SchemaError("Changing evidence metadata on an existing node requires expected_revision",
                          hint="Read RETURN n and pass its _revision, then retry the entire batch.")
    _ensure(engine, node.label)
    return {"previous": previous, "tracked": tracked}


def _record(engine: Engine, identity: str, sequence: int, value: dict,
            *, actor: str | None, reason: str | None, baseline: bool = False) -> None:
    metadata = EvidenceHistoryEntry(
        sequence=sequence, revision=content_revision(value),
        recorded_at=dt.datetime.now(dt.timezone.utc).isoformat(), source=value.get("_source"),
        actor=actor, reason=reason, baseline=baseline,
    )
    snapshot = canonical_json(_snapshot(value))
    metadata_json = metadata.model_dump_json()
    # Every new entry must remain readable within the maximum history-page
    # budget, even when a caller supplied a very large source or primary key.
    check_size("history_identity_bytes", len(identity.encode("utf-8")), 2048)
    check_size("history_metadata_bytes", len(metadata_json.encode("utf-8")), 16_384)
    new_bytes = len(snapshot.encode("utf-8")) + len(metadata_json.encode("utf-8"))
    check_size("history_snapshot_bytes", new_bytes, MAX_SNAPSHOT_BYTES)
    count, chars = engine.execute(f"MATCH (h:{HISTORY_TABLE}) RETURN count(h), coalesce(sum(size(h.snapshot) + size(h.metadata)), 0)").rows[0]
    node_count = engine.execute(f"MATCH (h:{HISTORY_TABLE}) WHERE h.node_id=$node RETURN count(h)", {"node": identity}).rows[0][0]
    hint = "History capacity is full; preserve this database and continue in a new graph. Existing history is never silently evicted."
    check_size("history_entries", count + 1, MAX_HISTORY_ENTRIES, hint=hint)
    check_size("history_node_entries", node_count + 1, MAX_HISTORY_PER_NODE, hint=hint)
    # Four bytes per existing Unicode character is a conservative storage bound.
    check_size("history_storage_bytes", chars * 4 + new_bytes, MAX_HISTORY_BYTES, hint=hint)
    engine.execute_write(
        f"CREATE (h:{HISTORY_TABLE} {{id:$id, node_id:$node, sequence:$seq, snapshot:$snapshot, metadata:$metadata}})",
        {"id": canonical_json([identity, sequence]), "node": identity, "seq": sequence,
         "snapshot": snapshot, "metadata": metadata_json},
    )


def after_update(engine: Engine, node: UpsertNode, pk: str, prepared: dict | None) -> None:
    if prepared is None:
        return
    tracked = prepared["tracked"]
    rows = engine.execute(f"MATCH (n:{node.label} {{{pk}: $key}}) RETURN n", {"key": node.key}).rows
    current = rows[0][0]
    update = node.evidence
    assignments: dict[str, Any] = {}
    if not tracked:
        legacy = current.get("status")
        legacy = legacy.strip().casefold() if isinstance(legacy, str) else None
        assignments.update(_evidence_state=legacy if legacy in {"superseded", "retracted", "expired"} else "current",
                           _review_state="unreviewed")
    if update is not None:
        for field, prop in [("state", "_evidence_state"), ("review", "_review_state"),
                            ("expires_at", "_expires_at"), ("superseded_by", "_superseded_by")]:
            if field in update.model_fields_set:
                value = getattr(update, field)
                if isinstance(value, dt.datetime):
                    value = value.astimezone(dt.timezone.utc).isoformat()
                assignments[prop] = value
    if assignments:
        engine.execute_write(
            f"MATCH (n:{node.label} {{{pk}:$key}}) SET " + ", ".join(f"n.{k}=${k}" for k in assignments),
            {"key": node.key, **assignments},
        )
    current.update(assignments)
    prepared["current"] = current


def finish_update(engine: Engine, node: UpsertNode, pk: str, prepared: dict | None, *, chain_steps: list[str] | None = None) -> None:
    """Validate the final batch graph, then record the node's committed snapshot."""
    if prepared is None:
        return
    previous, current = prepared["previous"], prepared["current"]
    tracked = prepared["tracked"]
    identity = make_node_id(node.label, node.key)
    update = node.evidence
    successor = current.get("_superseded_by")
    if successor:
        if current.get("_evidence_state") != "superseded":
            raise SchemaError("superseded_by requires state='superseded'; clear it when restoring current evidence")
        label, key = split_node_id(successor)
        pks = _node_pks(engine)
        if label not in pks or not key or successor == identity:
            raise SchemaError("superseded_by must identify another existing node")
        seen = {identity}
        if chain_steps is None:
            chain_steps = []
        while successor:
            chain_steps.append(successor)
            check_size("supersession_steps", len(chain_steps), 1024)
            check_size("supersession_depth", len(seen), 128)
            if successor in seen:
                raise SchemaError("Evidence supersession must not form a cycle")
            seen.add(successor)
            label, key = split_node_id(successor)
            if label not in pks:
                raise SchemaError("superseded_by target has an unknown label")
            projection = "n._superseded_by" if "_superseded_by" in _table_columns(engine, label) else "NULL"
            hits = engine.execute(f"MATCH (n:{label} {{{pks[label]}:$key}}) RETURN {projection}", {"key": key}).rows
            if not hits:
                raise NotFoundError(f"Superseding evidence {successor} does not exist")
            successor = hits[0][0]
    # Identical content is idempotent; an explicit explanation/actor is itself
    # a recorded review action. Replay IDs short-circuit before this function.
    if tracked and content_revision(previous) == content_revision(current) and not (update and (update.actor or update.reason)):
        return
    if previous is not None and not tracked:
        _record(engine, identity, 0, previous, actor=None, reason="Baseline at history adoption; earlier authorship/edits unknown", baseline=True)
    sequence = int(previous.get("_evidence_seq") or 0) + 1 if previous else 1
    engine.execute_write(f"MATCH (n:{node.label} {{{pk}:$key}}) SET n._evidence_seq=$seq", {"key": node.key, "seq": sequence})
    current["_evidence_seq"] = sequence
    _record(engine, identity, sequence, current, actor=update.actor if update else None,
            reason=update.reason if update else None)


def read_revision(engine: Engine, identity: str, sequence: int, pk: dict[str, str]) -> NodeRecord:
    if HISTORY_TABLE in _table_index(engine):
        rows = engine.execute(f"MATCH (h:{HISTORY_TABLE} {{id:$id}}) RETURN h.snapshot",
                              {"id": canonical_json([identity, sequence])}).rows
        if rows:
            json_bytes(rows[0][0], MAX_SNAPSHOT_BYTES * 2, "history_snapshot_bytes")
            node = node_record_from_value(json.loads(rows[0][0]), pk)
            node.properties["_history_revision"] = sequence
            return node
    raise NotFoundError(f"No recorded evidence revision {sequence} for {identity}",
                        hint="Use get_context(history=true) to list available revisions. History starts at adoption.")


def read_history(engine: Engine, identity: str, req: ContextRequest, budget: int,
                 freshness: FreshnessReport | None) -> ContextResponse:
    from grag.retrieval.packing import measure_response

    rows = []
    if HISTORY_TABLE in _table_index(engine):
        rows = engine.execute(
            f"MATCH (h:{HISTORY_TABLE}) WHERE h.node_id=$node AND h.sequence < $before "
            "RETURN h.metadata ORDER BY h.sequence DESC LIMIT 21",
            {"node": identity, "before": req.history_before if req.history_before is not None else 2**63-1},
        ).rows
    entries = [EvidenceHistoryEntry.model_validate_json(row[0]) for row in rows]
    page = entries[:20]
    while True:
        more = len(page) < len(entries)
        resp = measure_response(ContextResponse(context="", freshness=freshness or FreshnessReport(),
            history=EvidenceHistory(node_id=identity, entries=page, next_before=page[-1].sequence if more and page else None),
            truncated=more))
        if resp.response_token_estimate <= budget:
            if entries and not page:
                break
            return resp
        if not page:
            break
        page = page[:-1]
    raise ConfigurationError("token_budget cannot fit one evidence history entry", hint="Increase token_budget to read the recorded source and review explanation.")


def current_predicate(engine: Engine, table: str, alias: str) -> str:
    """Apply lifecycle before ranking/shortlisting, using only declared columns.

    Managed expiry values are normalized UTC ISO strings. A final Python check
    also rejects malformed external/imported expiry values.
    """
    columns = _table_columns(engine, table)
    parts = []
    state = f"{alias}._evidence_state" if "_evidence_state" in columns else "NULL"
    legacy = f"{alias}.status" if columns.get("status") == "STRING" else "NULL"
    if state != "NULL" or legacy != "NULL":
        parts.append(f"NOT lower(trim(coalesce({state}, {legacy}, ''))) IN ['superseded','retracted','expired']")
    if "_review_state" in columns:
        parts.append(f"coalesce({alias}._review_state,'') <> 'disputed'")
    if "_document_state" in columns:
        parts.append(f"coalesce({alias}._document_state,'') <> 'obsolete'")
    if "_expires_at" in columns:
        parts.append(f"({alias}._expires_at IS NULL OR {alias}._expires_at = '' OR {alias}._expires_at > $evidence_now)")
    return " AND ".join(parts) or "true"
