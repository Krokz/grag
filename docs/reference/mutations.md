# Writes and retries

Commit related changes atomically and reconcile concurrent edits without losing later work.
{ .grag-lead }

## Atomic writes and retries


Both upsert calls commit their nodes, edges, history and receipt together.
Validation or statement failures roll back the transaction. If native commit or
rollback completion cannot be confirmed, `transaction_outcome_unknown` refuses
further writes until reopen: restart the owning server, preserve the files and
replay the same operation ID with the exact payload. Without a receipt, inspect
stored state first; the write may already have committed. `upsert_nodes` accepts an optional
`edges` array, using the `upsert_edges` shape; endpoints can be existing nodes or
nodes in the same call. This also works through `POST /api/nodes/upsert` and
Python `UpsertNodesRequest`. Schema definition stays a separate step.
This example assumes `Decision.body` and `INFORMS` are declared, and
`Component:storage` already exists:

```json
{
  "nodes": [{"label": "Decision", "key": "storage", "properties": {"body": "Keep memory local"}, "source": "session:design"}],
  "edges": [{"type": "INFORMS", "from_label": "Decision", "from_key": "storage", "to_label": "Component", "to_key": "storage", "source": "session:design"}],
  "operation_id": "session-design:save-storage-1"
}
```

## Retry a possibly committed write

For an ambiguous response, repeat the **exact same request and `operation_id`**.
The optional ID is scoped to the database and retained across restarts. A committed
retry returns the saved result with `replayed: true`, without writing again—even
if another edit happened later. Its revisions describe that original commit.
Changing the payload while reusing its ID returns an `operation_id_conflict`.
Use a new ID for a new intended edit. Ordinary upserts need no operation ID.

## Guard against competing edits

To avoid overwriting another agent's work, first query a whole entity
(`RETURN n`, or `RETURN r` for a relationship) and pass its computed
`_revision` as `expected_revision` on that upsert item. Use `"absent"` for
create-only writes. All preconditions check the state before this batch's first
write; a mismatch rejects the batch with `revision_conflict` (REST HTTP 409;
MCP returns a structured error code). Read the current state and reconcile before retrying.
Guarded or retryable writes return a `revisions` map keyed by canonical entity ID.
For untracked nodes, reverting to identical content can restore a previous token.
History-tracked nodes also include their increasing sequence. `_revision` is computed metadata,
so query the entity rather than a nonexistent `n._revision` column.

## Relationship revisions

Relationship revisions use `r2:` followed by a content hash. They survive logical
export/restore even when native table and row IDs change. The upsert's relationship
type and endpoint keys select the entity; the token guards its type, properties
and provenance. Identical content on different endpoint pairs can share a token.
This needs no endpoint lookup for `RETURN r`, including relationships nested in
paths or maps. Duplicate relationships with the same type/endpoints still require
reconciliation before an upsert.

For new edits, legacy unprefixed relationship tokens return `revision_conflict`
with a reread hint. Reconcile against the new `r2:` value and use a new operation ID.
An exact retry of an already committed request still replays its saved receipt,
including legacy revision strings. Node revision format and history are unchanged.

## Property patches and warnings

Primary keys belong in `key`, never `properties`; invalid keys and unknown
request fields are errors. Undeclared, reserved, or mismatched properties retain
the existing skip-and-warn behavior, checked before writes; read `warnings`.
`null` clears a declared property. Existing embeddings are invalidated in the
same transaction when their source text changes.

## Receipts and backup continuity

Retry receipts live in the `.lbdb` file and are retained without automatic expiry.
Format-2 JSONL export/import preserves receipts and their exact replay, including
original revision tokens. Requests committed after the snapshot are absent from
the restored graph. Legacy v1 imports have no receipt continuity. A direct Engine
caller can join upserts to an existing
transaction, whose commit determines success; operation IDs require a top-level
upsert. A failed joined upsert invalidates that enclosing transaction.

[Authored correction history →](../guides/history.md)
