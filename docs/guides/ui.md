# Review your project memory

!!! note "Added in 0.11.0"
    Memories and Health use your existing graph and memory schema.

Open the URL printed by `grag status`, or start the local UI with `grag serve`.
The browser uses the same owning server as connected agents. Select the intended
database before inspecting or editing records.
The UI opens on **Graph**. Choose **Memories** to browse and maintain saved context,
or **Health** to inspect indexing and ingestion.

## Find what matters

| View | Use it for |
|---|---|
| **Memories** | Search saved text, inspect decisions and concepts, and correct records. |
| **Graph** | Follow relationships, run read-only Cypher and export a drawing. |
| **Health** | Inspect the selected database's code roots, optional embedding worker and recent ingestion jobs. |

Within Memories, **Open tasks** shows Task records with familiar unfinished
statuses: open, todo, pending, ready, active, in_progress, in-progress and blocked.
Other status conventions remain inspectable by choosing **Memories → Task**.

**Recent changes** sorts by each memory's latest recorded history entry, falling
back to its labelled creation time. Raw writes and source re-ingestion are not a
complete audit trail. Use a record's History tab for its individual revisions.

The type selector supports your existing schema, including custom memory labels
and primary keys. **All memory types** excludes standard source labels and managed
ingestion records. Select Module, Function, Chunk or another type explicitly to
inspect those records. No schema migration or semantic enrichment is required.

!!! note "An empty memory list can be expected"
    A project may have indexed code and documents without authored decisions or
    concepts. Ask your agent to save useful findings with their sources as you work.
    The UI does not invent semantic records to fill the list.

## Inspect evidence

Select a record to read its content, source citation, lifecycle and review state.
Web sources open as links; local file citations can be copied into your editor.
**Load linked evidence** shows a bounded neighborhood of stored relationships.
**Explore relationships** opens the record in the graph explorer.

Code coverage appears as a summary with expandable unresolved sites and parser
limitations. Raw properties stay available in a collapsed section. Neither a
relationship nor an accepted review state independently verifies a claim.
Generated code and document records show **Indexed source** instead of a default
unreviewed/history badge. Static extraction records what the source contains;
it does not verify the source's claims or prove complete parser coverage. Explicitly
recorded reviews, disputes, retirement, expiry and obsolete-source warnings remain
visible. Authored decisions and concepts retain their review status.

Inactive evidence is hidden from the list by default. Enable **Include inactive
evidence** to inspect retracted, superseded, disputed, expired or obsolete records.
Code-index freshness refers to registered source verification, not memory accuracy.
The Graph header follows the server's observed index status without triggering
extra scans. Hover it to see the verification time and the original graph/schema
read's freshness. A completed check does not automatically reload the canvas;
rerun your query or reset the view when you need updated graph contents.

## Correct, review or retire

1. Open an authored record and choose **Edit memory**, **Review** or **Retire memory**.
2. Change the relevant text fields or choose a review outcome. Supply a reason.
3. Choose **Save change**. The UI checks the revision and retains previous content
   in history. A blank source field preserves the current citation.

Saved edits update any loaded copy of the record in Graph, preserving its position
and relationships. Returning to Graph shows the saved values without reloading the
page. Expanding a node also picks up its latest stored properties.

Retirement removes a memory from current retrieval while preserving its content,
relationships and history. It does not complete a task. **Restore memory** restores
the lifecycle; existing expiry and disputed-review qualifiers still apply.

If another writer changed the record, the UI keeps your draft and refuses the
overwrite. Reload the latest record and reconcile your change. If the connection
drops during a save, **Retry same save** sends the original operation ID and exact
payload so a committed write is not applied twice.

Edits are attributed to `grag UI`; this is a supplied attribution, not an authenticated
person's identity. Generated code/document records are inspected here; change their
source and re-ingest to update generated content. The current form edits declared
STRING properties, not schema, keys or arbitrary typed values.

## Read history and limits

History starts when tracking is enabled, with a baseline for an existing record.
Earlier authorship is shown as unknown. The History tab pages older entries and
opens recorded snapshots. Large snapshots clearly indicate omitted content;
[revision text paging](history.md) through MCP/CLI retrieves the full text.

List previews are short excerpts; search checks the full stored STRING fields.
Paging and shared work/response limits keep reads bounded. If a large scan reaches
a limit, narrow the type or search text. A failed read is an error, not an empty list.
Dates and counts describe the captured read; refresh after changes from other agents.

Whole-node inspection uses the existing bounded query API. Records larger than its
reply limit require MCP/CLI projected reads or text paging. Numeric keys outside
JavaScript's safe integer range are not editable in the browser.

**Export SVG view** saves the loaded graph view. **Export full SVG** captures every
user node and edge through a separate topology download, then lays it out locally.
Browser memory/layout time and temporary disk capacity still constrain very large
drawings. Use `grag export` for a restorable database backup.
