<!-- grag-managed skill reference: memory -->
# Evidence and correction history

Use the graph's existing vocabulary. Common choices are Task (status and acceptance),
Decision (choice, reason and rejected alternative), Insight (established finding),
Question (unresolved issue), and Concept/Integration. They are conventions, not built-in
schema. Check `describe_schema`; define only missing types and endpoint pairs.

To track authored corrections, add `evidence: {}` to a node upsert. Adopting an
existing node needs `expected_revision` and retains its old value as baseline
sequence 0 with unknown earlier authorship. Later upserts retain snapshots and
sources atomically, even when they omit `evidence`. Node `_evidence_seq` increases;
identical writes add no history unless actor/reason records a review action.

Evidence fields are explicit patches:

| Field | Meaning |
|---|---|
| `state` | current, superseded, retracted |
| `review` | unreviewed, accepted, disputed; acceptance is the caller's judgment |
| `actor`, `reason` | Who supplied this edit/review and why; attribution is not authentication |
| `expires_at` | Zoned timestamp; null clears; no inferred TTL |
| `superseded_by` | Replacement `Label:key`; requires superseded state; null clears |

Omission preserves metadata; omitted actor/reason leaves this edit's attribution
unknown. Supersession targets must exist, and cycles reject the entire batch.
To restore superseded evidence, set current and explicitly clear its pointer.
Save replacement/old-node patches and links together with guards. Use relationships
to explain disagreements; grag does not automatically decide which claim wins.

Search/context `current` excludes explicitly superseded/retracted/expired/disputed
and retained obsolete source/document nodes. `all` includes their qualifiers.
Legacy statuses superseded/retracted/expired are recognized; ordinary Task open/done
statuses are unaffected. Current eligibility never independently verifies a claim.
`excluded_evidence` counts encountered exclusions, not every filtered database row.

## History and long text

`get_context(node_ids=["Label:key"], history=true)` returns up to 20 revisions within
the budget. Continue using `history_before=history.next_before` until null.
`revision=<sequence>` retrieves one stored node snapshot and supports `text_property`
paging. Both require one ID and skip expansion. Page with the returned hash and
character offsets; restart when the text changes. An excerpt or suffix is partial.

History starts at adoption. Past relationship topology, arbitrary raw writes,
relocations and imports are not authored review events. Untracked identical content
can yield a previous revision token; tokens are content preconditions, not clocks.
History-tracked node revisions include their sequence.

## Retries and revision compatibility

An `operation_id` (1–128 characters) identifies one exact request. Receipts persist
across restarts and format-2 restore, never silently expire, and replay before guard
checks. A repeated ID with a different payload conflicts. A replay's revision map
belongs to its original commit; read again before editing subsequent state.

Relationship `r2:` tokens describe type, content and provenance, independent of
native row/table IDs. The upsert's type/from/to keys select the entity being guarded;
identical content can share a token across different endpoint pairs. Duplicate
relationships with that identity must be reconciled before upserting. Node tokens
retain their existing format. Legacy relationship tokens are not accepted for new
edits: reread and reconcile, then use a new operation ID. Old receipts, including
old revision strings, replay exactly even after restore; do not rewrite those receipts.

REST conflicts return 409; MCP sets `isError=true` with a stable code in its JSON
footer/structured content. Read hints before retrying. History/receipt capacity
failures preserve old data and reject the new batch; never delete internal rows to
make room. A snapshot does not free capacity. See operations for backup continuity.

`transaction_outcome_unknown` means completion could not be confirmed. Restart the
owning server and replay the exact operation ID/payload; without a receipt, inspect
stored state first. An interruption alone does not prove nothing was committed.
