# Save, recall and revise memory <span id="save-and-revise-memory"></span>

Ask your agent to remember a decision with its reason, supporting source and a
link to the relevant component. On the next session, ask for that decision and
its evidence. grag provides storage and retrieval; the harness decides when to
read or write. Start with `describe_schema` and reuse the project's labels and
records. `Task`, `Decision`, `Insight` and `Question` are conventions, not a
required schema.

| Intent | Existing MCP tools |
|---|---|
| Save a memory | `upsert_nodes`, optionally with related `edges` in the same atomic request; `evidence: {}` opts into history. |
| Recall it | `search_knowledge` for discovery; `get_context` for known IDs and relevant neighbors. |
| Correct it | Read the whole entity with `cypher_query`, reconcile its content, then `upsert_nodes` with its `_revision` as `expected_revision`. |
| Retire it from current answers | A guarded `upsert_nodes` evidence patch with `state="retracted"` and `superseded_by=null`; content, links and history remain. |

Keep the canonical `Label:key` from results; keys may contain additional colons.
Confirm the selected database and any declared project membership before writing.
A source citation records provenance, not project ownership. Existing memory
labels and relationships remain usable without creating a separate `Memory` copy.

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
