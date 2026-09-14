# Operate grag

Keep a local graph available, understand failures and preserve saved knowledge.
{ .grag-lead }

<div class="grid cards" markdown>

-   **Share one graph**

    Connect several harnesses to the same owner, or serve separate databases.

    [Servers and clients →](server.md)

-   **Find a failure**

    Start from a symptom and check the selected command, database and client.

    [Troubleshooting →](troubleshooting.md)

-   **Protect saved knowledge**

    Export a verified snapshot and restore into a separate database.

    [Backup and recovery →](recovery.md)

-   **Move a checkout**

    Reconcile saved paths and registrations after reorganizing folders.

    [Relocation guide →](../guides/projects.md#move-a-checkout)

</div>

## Useful first checks

```bash
grag status
grag doctor
```

`status` identifies the selected database and owner. `doctor` checks installation
readiness. A healthy server does not establish that a particular agent's MCP
connection works; see [client verification](troubleshooting.md#discover-the-installation-and-saved-client).

!!! warning "Database cannot open?"
    Preserve the database, WAL and shadow files. Follow
    [separate-copy recovery](recovery.md#recover-a-database-that-cannot-open)
    before attempting to rebuild an index.
