# Resume work across sessions

Ask your agent: **“Use grag to resume the next open task for this checkout. Show
its acceptance criteria, next action, decisions and unresolved questions.”**
If you name a task, that choice takes precedence over a stored priority list.

The grag skill guides this through the existing schema, query, context and
upsert tools. Task selection remains the agent's responsibility. There is no
required task schema, extra service or automatic background task runner.

## Find the right work

1. **Confirm the project and checkout.** Use the saved database mapping and
   inspect the schema. Two folders named `repo` can have different work. A shared
   database needs declared project/checkout membership; a source citation alone
   is not proof of task ownership. Missing scope or a failed connection means
   uncertainty, not an empty task list. See [projects](projects.md).
2. **Query a small, explicit shortlist.** Use the project's unfinished statuses
   and priority convention. Prefer projected properties to long task bodies.
   Mission numbers, task IDs and search scores do not establish priority. Where
   the schema has no priority field, consult a current priority decision or the
   user's instruction. Report unknown priorities and ties; do not invent them.
3. **Check current evidence and fetch the handoff.** Use `get_context` for the
   candidate tasks, then expand the selected task to relevant decisions and
   questions. Inspect lifecycle, status, scope, omissions and expansion limits.
   A task without linked decisions or questions can still be valid work.
4. **Explain what to do next.** Give the task, reason for selection, acceptance
   check, next action and unresolved blockers. Separate blocked tasks from work
   that can proceed. Missing labels, a truncated shortlist or an empty bounded
   read do not establish that all work is complete.

For example, **only if these fields exist** and lower numbers mean higher
priority, an exact shortlist could be:

```cypher
MATCH (t:Task)
WHERE t.checkout_id = 'selected-checkout-id'
  AND t.status IN ['open', 'in_progress']
RETURN t.id, t.title, t.status, t.priority, t.summary
ORDER BY CASE WHEN t.priority IS NULL THEN 1 ELSE 0 END, t.priority, t.id
LIMIT 8
```

Use the actual checkout ID and declared properties. The ID sort stabilizes the
display; it does not resolve a priority tie. Null priority still needs a decision.
If this limited shortlist has no eligible candidate, continue through the work
list before making a broader absence claim.

Cypher is unfiltered. A task saying `open` may have superseded or expired evidence.
Use `get_context(node_ids=[...], hops=0, token_budget=1600)` to check the shortlist,
then `get_context(node_ids=["Task:selected-id"], hops=1, token_budget=1600)` for the
selected handoff. If a higher-priority candidate was omitted by the budget, read
it separately before choosing a lower-priority visible one. Follow partial text
with the existing pager when necessary; see [retrieval](retrieval.md).

`evidence="current"` excludes explicitly obsolete, superseded, retracted, expired
and disputed evidence. It still permits done tasks and answered questions, and
does not certify unreviewed claims. Check question status as well as lifecycle.
Code `freshness="require"` verifies registered source, not authored priorities,
acceptance results or release facts.

## Leave a useful handoff

Replace the task's current summary or body when work changes. Keep the current
status, acceptance result, next action, blockers and release state concise, using
the fields the project already declares. A task can be done locally and still
unreleased; the running grag package version does not prove a change shipped.

Read the whole task for its `_revision`, then send a sourced `upsert_nodes` update
with `expected_revision`. Add `evidence: {}` to preserve a legacy baseline and
subsequent correction history, or supply an actor and reason. Reuse the same ID
and preserve unrelated properties. On conflict, reread and reconcile before
retrying. Check returned warnings: undeclared properties are skipped, and
`define_schema(if_not_exists=true)` does not add columns to an existing table.

Keep older planning in [correction history](memory.md), rather than appending
every session's progress to the current body. History begins at adoption; it
cannot recover text overwritten before that point. Link supporting decisions
and unresolved questions using the existing relationship vocabulary.

**New in 0.10.0:** when context needs trimming, the packer gives current
summaries, release facts, acceptance criteria and next steps space before longer
narrative. It preserves selected node order, source citations and evidence
qualifiers. It does not infer truth or rewrite stored prose; full-fit responses
still include all properties. Clear handoffs and explicit lifecycle checks remain
necessary.
