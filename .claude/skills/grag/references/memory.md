<!-- grag-managed skill reference: memory -->
# Capture, reuse and correct memory

Use the graph's existing vocabulary. Common choices are Task (status and acceptance),
Decision (choice, reason and rejected alternative), Insight (established finding),
Question (unresolved issue), and Concept/Integration. They are conventions, not built-in
schema. Check unfamiliar schema before writes; define only missing types and
endpoint pairs. Topic search needs no schema preflight.

## Preserve the result of an investigation

Before finishing a useful investigation or handoff, save what a later session would
otherwise rediscover. Reuse an existing record and its summary/body fields; this is a
content convention, not a new schema or a required set of columns.

Preserve explicit user decisions and their reasons, even when they are only in the
conversation. A code observation alone cannot retain the user's rationale. Keep
the agreed choice and its discussion source distinct from what source inspection
established and from proposals still under consideration. Separate records or clear
clauses in an existing record both work; do not impose new labels to achieve this.

| Include when known | Purpose |
|---|---|
| Finding or decision, with its reason | Answer the next session's likely question directly. |
| Applicable component, project and conditions | Keep a local conclusion from becoming a universal rule. |
| Supporting source/symbols and what was checked | Make the conclusion traceable; record a revision only if actually observed. |
| Limits, uncertainty or rejected alternative and reason | Preserve qualifications; distinguish proposals from agreed decisions. |
| Unfinished work and next validation | Let implementation resume without inventing completion. |

Keep the decisive qualification beside the conclusion. A compact paragraph can
hold the whole result; a transcript, copied source body or growing session diary
usually cannot. Save only information supported by the investigation or an explicit
user decision. Do not invent alternatives, test results, authorship or review.
An agent's interpretation stays labeled as such; successful storage is not review.
Set each node's `source` beside `key` and `properties`, never in `properties` or as
`_source`. Cite the actual discussion for an agreed decision and the inspected
file/symbol for an implementation observation; one does not establish the other.
Use a discussion link when available; otherwise identify the conversation by its
date/topic and retain the relevant statement faithfully. Never invent a message URL
or use a code citation as the sole evidence for a user choice. Both sources can fit
the existing `source` string or be explained beside the claims in the body.
Link existing code/task nodes where useful. Repair skipped-property warnings rather
than claiming a complete save with missing provenance or qualifications.
If a finding changes, reconcile and replace its current summary with a revision
guard; history retains the earlier wording. Skip duplicate static facts and routine
progress. Do not manufacture a memory just to finish a turn.

## Let a memory replace repeated work

For prior rationale, constraints or a continuation, start with a focused topic
lookup using known labels, or `get_context` when the ID is already known. Search
does not require a schema preflight. Stop when the relevant result supplies the
requested claim, scope, source and qualifications. A follow-up read should resolve
a specific gap: omitted text or relationships, conflicting evidence, current-code
verification or the current whole entity/revision needed for an edit. A different
read tool returning the same record is not independent confirmation.

- **Recall:** if the record establishes the requested past decision/finding and
  its limits, answer with its source and status. Reopening implementation files
  is unnecessary unless the question also asks whether they still follow it.
- **Current implementation or edits:** use the memory to retain rationale and
  narrow the investigation. Check the relevant current source/tests before
  asserting behavior or changing code. Code freshness does not certify memory.
- **Missing, conflicting or partial evidence:** retrieve the particular omitted
  qualification, or investigate the affected area. An off-topic hit does not
  justify a chain of broader searches. Preserve uncertainty when scope or support
  cannot be established; do not treat eligible or accepted evidence as infallible.

## Correct a finding without changing the decision

When memory maintenance is already authorized, correcting a verified stale finding
is part of that work; do not ask for the same permission again. A read-only request
or narrower user scope still controls. Evidence that code changed does not authorize
changing the user's decision or repairing the code itself.

Read the whole current entity and reconcile the affected claim. Preserve the agreed
choice, its discussion source, unrelated fields and unresolved work. Describe the
current observation and its code source separately. Apply the correction to the same
record with `expected_revision` and `evidence: {}` (or an evidence patch with a reason)
so history retains the prior value. Do not promote review status without review.
Inspect skipped-property warnings; on a revision conflict, reread and reconcile.
If evidence or scope remains uncertain, report the discrepancy without presenting a
replacement as established. Exact lost-response retries follow the rules below.

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

For explicit CLI use or MCP fallback (see the main skill), CLI equivalents since
0.10.0 are `remember --track-history`,
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
