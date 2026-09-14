# Installation and client diagnostics

Distinguish the installed command, the selected database and the connection an agent actually uses.
{ .grag-lead }

[Find a symptom](troubleshooting.md) · [Installation](../installation.md)

## Discover the installation and saved client


**New in 0.10.0:** `status --json` and the expanded doctor report
identify the current Python executable, imported grag source, package locations,
PATH launchers, checkout mapping, database, owner, client configuration files and
installed skill bundles. This makes an editable/source installation distinguishable
from a published command with the same version number.

```sh
grag status --json
grag doctor --json
grag doctor --verify-client claude:project:grag
grag doctor --verify-client cursor:project:grag
```

## Choose a saved registration

Use the exact registration ID printed by discovery. Names such as `grag-algo3`
are included. Discovery reads Claude's project, user and local scopes, Cursor's
project and user scopes, and the user configuration for Windsurf/Zed. It honors
`CLAUDE_CONFIG_DIR`. Same-name precedence is shown among those files, following
[Claude Code's scopes](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence)
and [Cursor's configuration](https://cursor.com/help/customization/mcp).
Other profiles, managed/plugin/remote connector registrations, client approval
state and GUI environment are outside this inventory. Codex MCP remains separately
managed. Skill comparison checks the bundled files; it cannot establish which
copy a harness activated.

## Read-only discovery

Ordinary inspection does not launch a client, open the project database, schedule
index refreshes, delete stale PID registrations or change configuration. Doctor's
installation checks use disposable databases. A running owner that advertises
passive diagnostics can return its interpreter/source and cached indexed-root
observations. Missing observations and older owners remain **unverified**, not
evidence that the graph is empty or fresh. Malformed or moved mappings remain
visible alongside the rest of the report; they do not select a replacement graph.

## Verify the MCP connection

`--verify-client` explicitly launches the saved command, initializes MCP, lists
tools, describes schema and reads graph data. It also reads available `Repo.path`
and the recognized init marker's provenance. It sends no mutation tools or setup
marker, but launching the server can replay WAL and reads can trigger automatic
index refresh. A shared owner started by the registration may remain running.
This verifies that launcher in the diagnostic process's environment, not a live
GUI session or the completeness of saved memories. MCP does not attest its
filesystem path: the database association comes from the saved explicit selector.

The probe requires an existing local file explicitly selected by the registration
and matching this CLI's database. Use `grag --db /absolute/file.lbdb doctor
--verify-client CLIENT:SCOPE:NAME` when inspecting a different database. Missing,
implicit, URL and directory targets remain unverified rather than being guessed
or created. `--timeout` also bounds this probe (45 seconds by default). JSON
`ready` retains its **installation-only** meaning; `client_readiness` and
`verification` report the separate MCP result. Exit 1 also signals configuration
resolution or requested verification failure. An unavailable optional VECTOR
extension alone does not fail installation readiness.

## Review a repair

For a stale launcher, review `grag init --dry-run --client CLIENT` from the intended
checkout before applying. For a moved checkout, review `grag relocate OLD NEW
--dry-run`. These are repair previews, not automatic actions by doctor. Renamed or
user/local registrations may require targeted manual edits: init manages its own
`grag` entry, not every discovered name. Linked files are inspected with their
resolved targets reported; init continues to preserve links. No installation
actor is invented when provenance was never recorded.

## Interpret native failures

Native install results include the loaded binding/library paths and a pybind import
failure when fallback obscures it. Optional ONNX/FastEmbed package identity is
separate. Failed MCP probes retain bounded stderr and new owner-log output in JSON;
known credential values, headers and URL credentials/query strings are redacted.
The human report summarizes the failure and next step. WAL replay failure directs
you to separate-copy recovery; a native memory allocation failure is not reported
as invalid Cypher or corruption.
