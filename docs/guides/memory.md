# Save and revise memory

The CLI provides a small text-memory shortcut using the same selected
graph as MCP:

```bash
grag remember "Retry transient failures twice" --id retry-policy
grag search "retry" --json
grag context Memory:retry-policy --freshness require
```

`remember` uses a searchable `Memory(id,text)` table by default; `--label`, `--source`
and `--expected-revision` allow another compatible table, provenance and guarded
updates. Without `--id`, it creates a UUID. These commands do not add MCP tools.
The full schema-aware workflow below remains available for connected knowledge.


Ask your agent to describe the schema first, reuse the project's labels, and save
a decision with its reason and a source. For example:

> Remember that we keep customer exports local because the deployment must work
> offline. Link that decision to the export component.

On the next session, ask it to retrieve that decision and its evidence. grag
provides storage and retrieval; the harness decides when to read or write.

 `define_schema` refuses a new table whose name only differs from an existing one by case, plural or punctuation (`Decisions` vs `Decision`, `todo_item` vs `TodoItem`) and names the existing table in the hint; `allow_similar=true` creates it anyway. The packaged skill adds a suggested session-memory vocabulary (`Task`, `Decision`, `Insight`, `Question`) and the reuse-before-invent rule, without shipping a fixed schema.


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

For an ambiguous response, repeat the **exact same request and `operation_id`**.
The optional ID is scoped to the database and retained across restarts. A committed
retry returns the saved result with `replayed: true`, without writing again—even
if another edit happened later. Its revisions describe that original commit.
Changing the payload while reusing its ID returns an `operation_id_conflict`.
Use a new ID for a new intended edit. Ordinary upserts need no operation ID.

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

**Keep a memory's correction history.** Add `"evidence": {}` to its upsert item
to opt it in. On an existing node, supply `expected_revision`; grag preserves
the previous value as baseline revision 0, with unknown authorship. Later upserts
record snapshots with their sources, content revisions and increasing sequences
in the same transaction as the edit and retry receipt. Identical writes do not
add revisions; a supplied actor or reason records an explicit review action.

The optional evidence patch accepts `state` (`current`, `superseded`, `retracted`),
`review` (`unreviewed`, `accepted`, `disputed`), `actor`, `reason`, `expires_at`
(timestamp with timezone), and `superseded_by` (another canonical node id).
Omitted fields preserve values; null clears expiry or the supersession pointer.
Supersession requires `state="superseded"`; missing targets and cycles are
rejected. Change the old and replacement memories in one guarded upsert batch.
Review defaults to unreviewed. Accepted/disputed are explicit caller judgments;
actor attribution is supplied by the caller, not authenticated by grag.
No expiry is inferred and expiry never deletes data. Later edits without an
actor record an unknown updater, rather than crediting the previous author.

Read `get_context(node_ids=["Decision:storage"], history=true)` for a budgeted
list of revisions. Continue with `history_before=history.next_before` until it
is null. Use `revision=<sequence>` to retrieve a snapshot, optionally with
`text_property` paging. These modes use one node and skip expansion: historical
relationship topology is not recorded. History starts at adoption; it cannot
recover earlier overwritten text. Raw writes, relocation, and import do not
create authored review events. JSONL export/import currently omits internal
history; retain the original database for historical evidence. Ordinary nodes
need no history setup, preset schema, additional service or model.

Primary keys belong in `key`, never `properties`; invalid keys and unknown
request fields are errors. Undeclared, reserved, or mismatched properties retain
the existing skip-and-warn behavior, checked before writes; read `warnings`.
`null` clears a declared property. Existing embeddings are invalidated in the
same transaction when their source text changes.

Retry receipts live in the `.lbdb` file and are retained without automatic expiry.
JSONL export/import does not carry these internal receipts; treat an imported graph
as a new retry target. A direct Engine caller can join upserts to an existing
transaction, whose commit determines success; operation IDs require a top-level
upsert. A failed joined upsert invalidates that enclosing transaction.
