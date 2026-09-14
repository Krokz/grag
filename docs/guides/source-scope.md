# Select sources and ignore rules

Control what enters the graph and how later scans reconcile it.
{ .grag-lead }

[Start indexing](code.md) · [Move a checkout](projects.md)

## How selection works


Scanning honors `.gitignore` and `.gragignore` in the enclosing checkout and its
subdirectories. Rules use Git syntax, including negation; `.gragignore` is applied
after `.gitignore` in each directory. They apply to tracked and untracked files.
Global Git excludes are not used. Generated/VCS directories, symlinks and nested
repositories/worktrees are skipped. To index a nested repository, supply it as its
own directory root. Ignore rules never authorize following symlinks.

## Repositories that ignore everything by default

Explicit file arguments and `--root` still obey ignore rules. A leading `*` is
not enough to diagnose a skip: later exceptions and parent-directory rules matter.
An exception such as `!*.tf` cannot reopen a parent directory excluded by `*`.
The directories must be included too, as described in
[Git's ignore rules](https://git-scm.com/docs/gitignore#_pattern_format).

!!! note "Directory-negation fix in 0.10.0"

    grag 0.10.0 fixes a case where grag 0.9.0 prunes a directory even
    though a later `!*/` rule includes it. The allowlist examples here require
    that fix; explicit file arguments do not work around it.

For an intentionally excluded source tree that should be indexed by grag, use a
scoped `.gragignore` exception at the checkout root. For example, with a root
`.gitignore` containing `*`, this selects `.tf` files under `terraform/`:

```gitignore
/terraform/**
!/terraform/
!/terraform/**/
!/terraform/**/*.tf
/terraform/**/.terraform/
```

The first rule excludes the tree's contents; the exceptions allow traversal and
Terraform source files. State files, `.tfvars`, other file types and the downloaded
`.terraform/` directory stay excluded. Adjust `terraform/` to the intended source
root and inspect any more-specific policies. Confirm the resulting module paths
and ingest warnings; a successful call alone does not establish coverage.

## Explicit source roots

A directory argument remains its own `Repo` root. Explicit files use their enclosing
checkout (or their parent in a plain folder). Use `--root` to group selected paths
under a chosen enclosing root without indexing its other files:

```bash
grag ingest-code --root . src/api.py tests/test_api.py
# Replace that root's registered scope; omitted generated code is reconciled.
grag ingest-code --root . --replace-scope src/api.py
# Remove the registered code scope, preserving authored references/memories.
grag ingest-code --root . --replace-scope
```

MCP accepts `root` and `replace_scope` on `ingest_code`; an empty `paths` list with
both fields removes the scope. Ordinary re-ingests retain earlier registered paths
under the same root. [Freshness](freshness.md) uses exactly the saved selection.
New exclusions, including the file-size threshold, reconcile generated nodes and
edges. Authored/unknown relationships remain; referenced obsolete code nodes are
marked `_source_state="obsolete"` and omitted from current retrieval. Review the
warnings and use `evidence="all"` or Cypher to inspect retained evidence.
