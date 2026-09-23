# Save, recall and revise memory <span id="save-and-revise-memory"></span>

Keep supported conclusions from useful investigations so the next session can
reuse the result. grag provides storage and retrieval; the harness decides when
to read or write. The packaged guidance encourages capture at a useful conclusion,
decision, correction or handoff. You can also explicitly ask it to remember.
{ .grag-lead }

Reuse the project's labels and records; check unfamiliar schema before writes.
Topic search needs no schema preflight. `Task`, `Decision`, `Insight` and `Question`
are conventions, not a required schema.

| Intent | Existing MCP tools |
|---|---|
| Save a memory | `upsert_nodes`, optionally with related `edges` in the same atomic request; `evidence: {}` opts into history. |
| Recall it | `search_knowledge` for discovery; `get_context` for known IDs and relevant neighbors. |
| Correct it | Read the whole current entity with `get_context`, reconcile, then `upsert_nodes` with its `_revision` as `expected_revision` and `evidence` set. Check that `history` says `recorded`. |
| Retire it from current answers | A guarded `upsert_nodes` evidence patch with `state="retracted"` and `superseded_by=null`; content, links and history remain. |

Keep the canonical `Label:key` from results; keys may contain additional colons.
Confirm the selected database and any declared project membership before writing.
A source citation records provenance, not project ownership. Existing memory
labels and relationships remain usable without creating a separate `Memory` copy.

## Save a conclusion the next session can use

A compact memory should establish the finding or decision, its reason and scope,
supporting evidence, important limits and any unfinished work. Fit these into
existing fields such as `summary` or `body`; no new table or columns are required.
Keep decisive qualifications beside the conclusion so a partial read cannot easily
separate them. Link relevant code and tasks when the schema supports it.

!!! example "Illustration: request-scoped identity"
    **Decision:** keep user identity on each request because the executor is shared.
    **Reason:** mutable executor credentials could leak identity between requests.
    **Evidence:** the accepted design discussion and inspected executor symbol.
    **Unfinished:** downstream delegation and concurrent-request rejection tests.

    This is a fictional example, not an observation about your project. A real
    memory should cite actual evidence and only record checks that were performed.

Save observed behavior, agreed decisions and tentative proposals as distinct claims.
Preserve explicit user decisions and their reasons even when they exist only in the
conversation; a code observation cannot replace that rationale. Cite the discussion
for the choice and the inspected code for observed behavior.
If no discussion link is available, identify the conversation by date/topic and
retain the relevant statement faithfully; do not invent a link. The existing
`source` field and memory body can carry both forms of provenance.
An agent's inference is not an accepted decision. Successful storage does not mean
review; preserve review state and attribution honestly. Replace changed summaries
with revision guards and retain history, instead of appending an endless diary.

### The capture step

The packaged guidance now names the moment and the mechanics: when the user states
an agreement or decision, or a session establishes a reusable finding, the agent
saves it before the final answer unless the request is read-only (`/grag capture`
or a handoff request runs the same step on demand).

1. One `search_knowledge` with explicit memory labels (`top_k=4, hops=0`). An
   explicit zero in `label_hits` for each means no eligible candidate matched.
   If `excluded_evidence` is above zero, check `evidence="all"` before saving;
   this also applies when the reply contains unrelated records. Zero exclusions
   cannot rule out vector-only hidden matches or matches beyond the lexical
   shortlist. A label in `unknown_labels` does not exist in this graph; check
   the name or the database rather than repeating.
2. A matching record is read whole (`get_context`; every packed node carries
   its `_revision`) and updated under that token as `expected_revision`, with
   `evidence` (even `{}`) so the prior body is retained. The response's
   `history` map reports per node: `created`, `recorded` (prior version
   recoverable), or `not_recorded` (overwritten without history — a guard
   alone does not retain it).
3. Otherwise `upsert_nodes` creates the record with a scalar `key`,
   `expected_revision: "absent"`, `evidence: {}`, a `source` naming both the
   discussion and the inspected file, and a body holding the claim, scope,
   qualification, unchosen proposal and next step. Only relationship types the
   schema lists are used; when none fits, the related record is named in the body.

The memory reference in the packaged skill carries a complete valid payload.
Write errors now say how to repair the two mistakes seen in evaluation: a `key`
given as an object instead of the scalar value, and an edge whose relationship
type is not defined when schema changes are not permitted.

## Reuse it without repeating the investigation

| Next-session question | Appropriate use |
|---|---|
| Why did we choose request-scoped identity? | Retrieve the decision and answer from its supported rationale and qualifications. |
| Does today's implementation isolate identity? | Use the memory to locate the relevant source and verify current behavior. |
| Continue the delegation work | Retrieve the task's current status, constraints and next check; inspect the code being changed. |

A focused `search_knowledge` or a `get_context` for a known ID may supply all the
evidence a recall question needs. Stop when the relevant result establishes the
requested claim, scope, source and qualifications. Follow-up reads should resolve
missing evidence, a conflict, current-code verification or an edit's revision guard.
Reading the same record through another tool does not independently confirm it.
Memory freshness is not established by code freshness alone.

The benefit to look for is an investigation the agent can skip while still answering
correctly. Shorter tool output alone does not establish lower session cost or tokens.
These instructions support reuse; they do not guarantee harness behavior or savings.

## Correct a stale finding

Within an authorized memory-maintenance task, a verified correction can proceed
without another permission request. Read-only scope still controls. Read the whole
current record, preserve unrelated claims and the user's original decision, and
update the observed implementation with its current source. A divergence in code
does not mean the user changed their decision or authorized a code edit.

Use the same record, its current `expected_revision` and `evidence: {}` (or an
evidence patch with a reason) to retain history. Check warnings and reconcile
revision conflicts. Preserve review status; report unresolved discrepancies when
evidence is insufficient for a supported correction.

## CLI shortcuts

The CLI uses the same [database resolution](../reference/configuration.md#database-selection)
and owning server as MCP. **New in 0.10.0:** `inspect`, `retire` and
the history/retry options below complete the text-memory workflow:

### Save a memory

```bash
grag remember "Retry transient failures twice" --id retry-policy \
  --track-history --source design.md --json
```

`remember` uses a searchable `Memory(id,text)` table by default. `--label` selects
another compatible table; `--source` supplies provenance (default `grag remember`).
Without `--id`, it creates a UUID. `--track-history` or `--reason` opts into
correction history and defaults to create-only: an existing ID needs its current
`--expected-revision`. Plain `remember --id ...` retains its existing unguarded
upsert behavior. Prefer a guard when other sessions may edit the same memory.

### Recall it

```bash
grag search "retry" --json
grag context Memory:retry-policy
```

### Inspect and correct

```bash
grag inspect Memory:retry-policy --json
```

Read the inspected content and use its `revision` token in the following edit
(replace the placeholder with the returned value):

```bash
grag remember "Retry transient failures three times" --id retry-policy \
  --expected-revision TOKEN_FROM_INSPECT \
  --reason "Updated retry requirement" --source review.md --json
```

`inspect` reads one whole node, including its current revision and evidence state,
with ordinary query response/work limits. It is unfiltered by evidence eligibility
and is not a token-budgeted context read. It also supports custom labels and primary
keys, such as `Decision:storage`. Its `--freshness require` option verifies registered
code sources, not the truth of an authored memory. A history snapshot's token may
be outdated; inspect current state before editing, and reconcile on a conflict.

### Retire from current answers

Inspect again after the edit to get its current revision:

```bash
grag inspect Memory:retry-policy --json
grag retire Memory:retry-policy --expected-revision LATEST_TOKEN_FROM_INSPECT \
  --reason "Requirement withdrawn" --json
```

`retire` requires an existing revision token and never creates a missing node.
It preserves content, relationships and provenance (unless `--source` is supplied),
starts or extends correction history, and clears a previous supersession pointer.
Retraction excludes the memory from default search/context; `--evidence all`
allows qualified inspection. It is not permanent deletion and does not mark a
Task done or resolve a Question. To restore evidence through MCP, inspect it and
use a guarded patch with `state="current"` and `superseded_by=null`; review any
remaining expiry or disputed review state before expecting current retrieval.

### Read earlier versions

```bash
grag context Memory:retry-policy --history --json
grag context Memory:retry-policy --revision 1 --json
grag context Memory:retry-policy --evidence all --json
```

History uses one ID and the same `--tokens` budget as context. Continue with
`--history-before` set to `history.next_before` until null. `--revision` selects a
snapshot sequence, not a write-guard token. New tracked memories start at sequence
1; adopting a legacy memory retains its previous value at baseline 0. History
cannot recover text overwritten before adoption or past relationship topology.

### Retry an interrupted write

For a potentially lost write response, `remember` and `retire` accept
`--operation-id`. `remember` also requires `--id` so retry targets stay stable.
Repeat the exact arguments and operation ID: a receipt replays the original commit,
even after later edits, and does not make that old revision current. Use `inspect`
before a subsequent edit and a new operation ID for a changed payload.

These are CLI conveniences over the existing ten MCP tools. Use [schema-aware
upserts](../reference/mutations.md) for custom properties and connected writes.

For open tasks, acceptance checks and session handoffs, follow [Resume work](resume.md).

`define_schema` refuses a new table whose name only differs from an existing one by case, plural or punctuation (`Decisions` vs `Decision`, `todo_item` vs `TodoItem`) and names the existing table in the hint; `allow_similar=true` creates it anyway. The packaged skill asks agents to reuse existing labels and properties.



## Predictable schema names

Reuse existing labels, primary keys and relationship types. The
[schema reference](../reference/schema.md) explains valid names, similar-name
checks and compatible schema changes.

## Atomic writes and retries

Use `upsert_nodes` with optional `edges` to save connected knowledge atomically.
For transaction boundaries, revision conflicts, relationship tokens and receipt
continuity, read [writes and retries](../reference/mutations.md).

**Next:** [review history and evidence](history.md) or [resume a task](resume.md).
