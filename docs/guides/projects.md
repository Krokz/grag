# Projects, worktrees and relocation

Run commands from the checkout root or a subdirectory: they select the same
database. `init` finds the enclosing Git root or existing grag project root.
Each new checkout/worktree gets its own identity and database, even when folder
names match. The local mapping is Git-ignored; do not commit or copy it between
worktrees. To intentionally share memory, run `grag --db /path/to/shared.lbdb init`
in each checkout and use one server for that database.

Existing absolute database paths in project Claude/Cursor registrations are
respected; rerun `init` to save the mapping. An existing root `knowledge.lbdb` can
also be adopted. A `~/.grag/<folder-name>.lbdb` file alone cannot establish which
checkout owns it: choose it explicitly with `--db <file> init`. Conflicting or
ambiguous registrations fail with an explanation rather than selecting a new graph.

## Move a checkout

After moving a folder, stop its server and disconnect clients that auto-start it:

```bash
grag relocate /previous/project /current/project --dry-run
grag relocate /previous/project /current/project
```

The command uses the destination's mapping; `--db <existing-file>` overrides it
for older installations. It updates indexed roots, `_source` citations, saved
indexing scope, and local Claude/Cursor launch paths while retaining node IDs,
memory text, and relationships. A moved grag virtualenv launcher is replaced with
the currently functioning grag command. The database stays where it is; if it was
inside the moved folder, the mapping selects its existing new path. With no
database present, a matching mapping can still be updated; this is reported and
no database is created. Copies use `init` for a fresh identity; relocation requires
the old directory to be absent.

Graph changes commit together. Client-file writes use the init backups described
below. If file publication fails after the graph commits, rerun the same command
to finish configuration. Dry runs open existing databases read-only. Restart clients
afterward; code freshness must verify the new location. Legacy indexes with no
saved options still need one explicit ingest with the intended scope. Re-run
`init --client <client>` for user-scope registrations; discovery/repair of linked
skills and arbitrary client installations remains separate work. Existing document
nodes retain their identities after relocation and subsequent document re-ingestion
since 0.9.0.

## Review and undo setup changes

`grag init` installs the complete skill bundle: a short `SKILL.md` plus references
for ingestion, memory history and operations. Agents load those procedures only
when needed. Rerunning init upgrades grag-owned files; an unrelated reference-file
collision is reported before applying changes. Removal retains modified references
and preserves the bundle if its entrypoint was customized.

Use `grag init --dry-run` (or `grag init --remove --dry-run`) to review the actual
diffs before applying. Diffs include changed configuration values. Init validates
JSON and the registration's object structure; invalid or unreadable files stop
the operation with their paths intact. Zed's JSONC comments and unrelated settings
are preserved. Duplicate JSON keys and ambiguous `CLAUDE.md` markers are refused.

Every apply stages its writes and preserves original bytes in
`~/.grag/backups/init/run-*/` before replacing or deleting a file. The command prints
that directory; `manifest.json` maps each numbered `.original` backup to its full
path, original permission mode, and before/after SHA-256 hashes. To restore a file,
close its client/editor, compare the current file with the manifest, copy that
file's `.original` back to the recorded path, and restore its recorded mode.
Entries with no backup describe newly created files. Backups are retained until
you remove them; on POSIX, backup directories/files are private to your user.

Each file replacement is atomic. A crash between files can leave a partially
applied setup: the manifest's hashes identify which files changed, and a separate
valid `complete.json` marks a finished run. Init rejects stale plans and overlapping
grag init writers; close external config editors during apply because they do not
share that lock. Symlinked/hardlinked config files are left intact for manual
configuration. Existing POSIX permission modes are preserved; custom ACLs and
extended file attributes are not copied to replacement files.
