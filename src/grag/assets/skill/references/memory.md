<!-- grag-managed skill reference: memory -->
# Evidence and correction history

Use the graph's existing vocabulary. Common choices are Task (status and acceptance),
Decision (choice, reason and rejected alternative), Insight (established finding),
Question (unresolved issue), and Concept/Integration. They are conventions, not built-in
schema. Check `describe_schema`; define only missing types and endpoint pairs.

## Everyday memory

| Intent | Existing tools |
|---|---|
| Remember | Reuse a stable record ID with `upsert_nodes`, a supporting `source`, and related `edges` when the schema provides them. `evidence: {}` starts history. |
| Recall | Use `search_knowledge` for discovery, `get_context` for known IDs and needed neighbors. Default evidence is current. |
| Correct | Read the whole entity with `cypher_query`, reconcile its content, then upsert with its `_revision` as `expected_revision`. Replace outdated summaries; history retains prior text. |
| Retire from current answers | Guard an evidence patch with `state="retracted"`, `superseded_by=null` and a reason. Preserve content and relationships. |

Confirm the selected database and declared project membership; a source path is
not scope proof. Use returned canonical `Label:key` IDs and keep additional colons
in keys. Prefer the existing project schema over a duplicate text-memory table.
Retraction is retained history, not permanent deletion or task completion. Do not
report data as erased after retraction. Restore through a guarded current-state
patch, clearing the supersession pointer and reviewing expiry/dispute qualifiers.

New in 0.10.0: optional CLI equivalents are `remember --track-history`,
`search`, `context`, `inspect Label:key --json`, and
`retire Label:key --expected-revision TOKEN`. `inspect` is an unfiltered whole-node
query with a current revision, not budgeted context. `remember --reason` also
enables history; without a guard, history-enabled remember is create-only. Plain
remember retains unguarded upserts. `retire` preserves provenance unless `--source`
is supplied. Use `context --history`, `--history-before`, `--revision` or
`--evidence all` for retained evidence. `remember/retire --operation-id` retries
must repeat the exact payload; remember also needs `--id`. A replay's token belongs
to the earlier commit, so inspect again before a new edit. Connected/custom writes
still use the same MCP tools; there are no separate recall/revise/forget APIs.

## Schema and evidence

New in 0.10.0: schema names use unquoted letters/digits/underscores
and reject native reserved words case-insensitively. Follow the name-specific
`schema_error` hint, e.g. rename `optional` to `optional_value`; backticks and
`allow_similar` do not bypass it. A failed schema batch rolls back its tables and
registry together. Correct and retry the whole request; on uncertain native
completion, reopen and inspect schema first. `if_not_exists=true` reuses existing
tables and does not add missing columns. Normal and custom typed keys still work.

To track authored corrections, add `evidence: {}` to a node upsert. Adopting an
existing node needs `expected_revision` and retains its old value as baseline
sequence 0 with unknown earlier authorship. Later upserts retain snapshots and
sources atomically, even when they omit `evidence`. Node `_evidence_seq` increases;
identical writes add no history unless actor/reason records a review action.
Set `source` to the evidence supporting the current edit; history retains earlier
sources even when the current source changes.

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
Attribute an edit to the agent performing it unless the user explicitly supplies
another actor; do not infer authorship from a login or an earlier revision.
To restore superseded evidence, set current and explicitly clear its pointer.
Save replacement/old-node patches and links together with guards. Use relationships
to explain disagreements; grag does not automatically decide which claim wins.

Search/context `current` excludes explicitly superseded/retracted/expired/disputed
and retained obsolete source/document nodes. `all` includes their qualifiers.
Legacy statuses superseded/retracted/expired are recognized; ordinary Task open/done
statuses are unaffected. Current eligibility never independently verifies a claim.
`excluded_evidence` counts encountered exclusions, not every filtered database row.

## Resume work

Use the user's selected task first. Otherwise:

1. Resolve the current checkout and database (operations reference); a same-named
   repo or a working MCP connection is not scope proof. Read `describe_schema`.
   In a shared graph, use declared project/checkout membership. A citation path
   is evidence provenance, not necessarily task ownership. Missing membership
   makes scope uncertain; do not silently choose another project's task.
2. Query a small shortlist by exact unfinished status and the project's explicit
   priority convention. Include ID, title, priority, status and current summary
   when declared. Mission numbers/IDs and search scores are not priority. Consult
   a current priority decision if the ordering changed; do not invent missing
   priority, directions or tie-breaking decisions. A stable ID sort is only for
   display. Separate blocked work from actionable work and surface blockers.
3. Read the chosen task's acceptance/next step and relevant linked decisions and
   open questions with `get_context` (usually hops=1, token_budget=1600). Cypher
   is unfiltered: check evidence lifecycle too; `evidence="current"` excludes
   obsolete/disputed evidence but still allows done tasks and answered questions.
   Inspect status, release facts, omissions and scope before acting. If a candidate
   is excluded by lifecycle, consider the next one. If budget/limits omit a higher-
   priority candidate, narrow or page the read before choosing a lower one. An empty
   bounded read is not exhaustive; check open questions for lifecycle eligibility too.
4. State the task, reason for priority, acceptance check, next action and unresolved
   blockers. Do not claim "nothing left" from failed reads, absent labels or a
   truncated shortlist. If priority/scope is absent, report the shortlist and the
   uncertainty. Code freshness does not verify authored task/release facts.

Keep current records concise on handoff: current summary (or body when that is the
schema), status, acceptance/next action, blockers and release state where declared.
`done` can mean implemented locally while still unreleased. A package version on
the running server does not prove a project change shipped. Replace outdated prose
with a guarded, sourced update; evidence history retains it. Reuse the same ID,
preserve unrelated fields and link supporting decisions/questions. Check skipped-
property warnings: these are conventions, not a required schema or an automatic
column migration. Do not create a duplicate Task table merely for these names.

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
