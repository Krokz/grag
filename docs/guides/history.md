# History and evidence

Retain corrections and their sources, and distinguish current claims from
superseded or retracted evidence.
{ .grag-lead }

[Everyday memory workflow](memory.md) · [Write and retry contracts](../reference/mutations.md)

## Start tracking a memory

Add `"evidence": {}` to its upsert item
to opt it in. On an existing node, supply `expected_revision`; grag preserves
the previous value as baseline revision 0, with unknown authorship. Later upserts
record snapshots with their sources, content revisions and increasing sequences
in the same transaction as the edit and retry receipt. Identical writes do not
add revisions; a supplied actor or reason records an explicit review action.

## Evidence fields

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

## Read revisions and snapshots

Read `get_context(node_ids=["Decision:storage"], history=true)` for a budgeted
list of revisions. Continue with `history_before=history.next_before` until it
is null. Use `revision=<sequence>` to retrieve a snapshot, optionally with
`text_property` paging. These modes use one node and skip expansion: historical
relationship topology is not recorded.

## What history preserves

History starts at adoption; it cannot
recover earlier overwritten text. Raw writes, relocation, and import do not
create authored review events. Format-2 JSONL snapshots preserve authored history;
legacy v1 exports did not. See [backup and restore](../operations/recovery.md)
for snapshot-point continuity and legacy import limits. Ordinary nodes
need no history setup, preset schema, additional service or model.



## Inspect retained evidence

Search and context default to current evidence. Use `evidence="all"` to inspect
superseded, retracted, expired or disputed claims with their qualifiers.
Retraction preserves content and history; it does not erase a memory.
For the CLI, use `context Label:key --history`, `--revision N` or `--evidence all`
(new in 0.10.0). [CLI memory options](../reference/cli.md#everyday-memory-and-retrieval)
list the exact flags.
