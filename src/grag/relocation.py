"""Explicit offline reconciliation after a checkout folder has moved.

Graph changes commit together. Client files use init's backed-up, checked writes;
if publication fails after the graph commits, rerunning completes those files.
No files or databases are moved, and no node keys or edge endpoints are changed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from grag.code_state import (
    index_records,
    options_json,
    registered_repo_ids,
    saved_request,
)
from grag.config import GragConfig
from grag.config_document import read_server
from grag.core.engine import Engine, is_internal_label
from grag.core.errors import GragError
from grag.core.ident import validate_identifier
from grag.project_files import (
    ProjectConfigError,
    WriteOp,
    apply_ops,
    preview_ops,
    snapshot,
)
from grag.project_identity import identity_ops, read_identity, rebase, registration_args


@dataclass
class GraphEdit:
    query: str
    params: dict
    description: str


def plan_graph(engine: Engine, old: Path, new: Path) -> list[GraphEdit]:
    records = index_records(engine)
    ids = registered_repo_ids(engine)
    moving = {
        root: rebase(root, old, new)
        for root in records
        if rebase(root, old, new) != root
    }
    for destination in moving.values():
        if destination in records:
            raise GragError(
                f"Destination {destination} already has an index; refusing to merge identities"
            )
        if not Path(destination).is_dir():
            raise GragError(f"Moved indexed directory is missing: {destination}")
    edits = []
    for table, kind in engine.execute("CALL SHOW_TABLES() RETURN name, type").rows:
        if is_internal_label(table):
            continue
        validate_identifier(table)
        columns = {
            row[1]
            for row in engine.execute(f"CALL TABLE_INFO('{table}') RETURN *").rows
        }
        match = f"MATCH ()-[n:{table}]->()" if kind == "REL" else f"MATCH (n:{table})"
        # _source is location provenance; prose and arbitrary authored properties
        # are deliberately untouched. Code `path` can be absolute in older graphs.
        props = {"_source"} & columns
        if kind == "NODE" and table in {
            "Repo",
            "Module",
            "Class",
            "Function",
            "TerraformModuleCall",
        }:
            props |= {"path"} & columns
        for prop in sorted(props):
            rows = engine.execute(f"{match} RETURN DISTINCT n.{prop}").rows
            for (value,) in rows:
                if not isinstance(value, str) or rebase(value, old, new) == value:
                    continue
                destination = rebase(value, old, new)
                edits.append(
                    GraphEdit(
                        f"{match} WHERE n.{prop} = $old SET n.{prop} = $new",
                        {"old": value, "new": destination},
                        f"{table}.{prop}: {value} -> {destination}",
                    )
                )
        if table == "Module" and "_ingest_hash" in columns and "_source" in columns:
            for destination in moving.values():
                edits.append(
                    GraphEdit(
                        "MATCH (n:Module) WHERE n._source STARTS WITH $prefix SET n._ingest_hash = NULL",
                        {"prefix": destination + "/"},
                        "Invalidate moved module fingerprints",
                    )
                )
    repo_columns = (
        {row[1] for row in engine.execute("CALL TABLE_INFO('Repo') RETURN *").rows}
        if records
        else set()
    )
    for root, destination in moving.items():
        record = records[root]
        settings = []
        params: dict = {"id": ids[root]}
        if record["_index_options"]:
            request = saved_request(Path(root), record["_index_options"])
            request.paths = [rebase(p, old, new) for p in request.paths]
            params["policy"] = options_json(request)
            settings.append("r._index_options = $policy")
        if "_index_generation" in repo_columns:
            settings.append("r._index_generation = NULL")
        if "_index_error" in repo_columns:
            settings.append("r._index_error = NULL")
        if settings:
            edits.append(
                GraphEdit(
                    f"MATCH (r:Repo {{id: $id}}) SET {', '.join(settings)}",
                    params,
                    f"Preserve {ids[root]} and its indexing options; require verification at {destination}",
                )
            )
    return edits


def plan_files(db: Path, old: Path, new: Path) -> list[WriteOp]:
    from grag.project import _BLOCK_START, _config_op, _grag_bin, plan_claude_md_op

    identity = read_identity(new, allow_moved=True)
    if identity and Path(identity.root) not in (old, new):
        raise ProjectConfigError(
            f"Destination mapping belongs to {identity.root}, not {old}"
        )
    mapped = identity and Path(rebase(identity.db_path, old, new)).resolve() == db
    ops = []
    for relative in (".mcp.json", ".cursor/mcp.json"):
        path = new / relative
        before = snapshot(path)
        if before.data is None:
            continue
        try:
            entry = read_server(before.text, "mcpServers")
            if not entry:
                continue
            args = registration_args(entry)
            if "--db" not in args or any(
                a == "--server-url" or a.startswith("--server-url=") for a in args
            ):
                continue
            selected = args[args.index("--db") + 1]
            if not Path(selected).is_absolute():
                raise ValueError(
                    "relative --db cannot be reconciled safely; rerun init with explicit --db"
                )
            if Path(rebase(selected, old, new)).resolve() != db:
                continue
            entry["args"] = [rebase(item, old, new) for item in args]
            if isinstance(entry.get("command"), str):
                command = entry["command"]
                relocated = rebase(command, old, new)
                # A moved venv's launcher may retain an old absolute shebang.
                # Use this functioning grag install for a known grag launcher.
                entry["command"] = (
                    _grag_bin()
                    if relocated != command
                    and Path(command).name in {"grag", "grag.exe"}
                    else relocated
                )
            op = _config_op(path, "mcpServers", entry)
            if op:
                ops.append(op)
        except (ValueError, IndexError, RecursionError) as exc:
            raise ProjectConfigError(f"{path}: {exc}; left intact") from exc
    if mapped and identity:
        instructions = snapshot(new / "CLAUDE.md")
        if instructions.data is not None and _BLOCK_START in instructions.text:
            ops.append(plan_claude_md_op(new, db, port=identity.port))
        ops.extend(identity_ops(new, replace(identity, root=str(new), db_path=str(db))))
    return ops


def relocate_checkout(
    config: GragConfig, old: Path, new: Path, *, dry_run: bool = False
) -> None:
    from grag.admin import find_server

    old, new = old.expanduser().resolve(), new.expanduser().resolve()
    if old == new or old.is_relative_to(new) or new.is_relative_to(old):
        raise ProjectConfigError(
            "Relocation needs distinct, non-overlapping old and new roots"
        )
    if old.exists():
        raise ProjectConfigError(
            "Old root still exists. For a separate copy/worktree, run init there; relocation is for a moved folder"
        )
    if not new.is_dir():
        raise ProjectConfigError(f"New checkout directory does not exist: {new}")
    if config.db_dir is not None:
        raise ProjectConfigError("Relocate one database at a time with --db <file>")
    db = config.db_path.expanduser().resolve()
    if not db.is_file():
        identity = read_identity(new, allow_moved=True)
        if (
            not db.exists()
            and identity
            and Path(rebase(identity.db_path, old, new)).resolve() == db
            and not any(
                Path(str(db) + suffix).exists() for suffix in (".wal", ".shadow")
            )
        ):
            ops = plan_files(db, old, new)
            if dry_run:
                preview_ops(list(ops))
            else:
                apply_ops(list(ops))
            print(
                f"Database absent at {db}; configuration {'previewed' if dry_run else 'updated'} only. No database was created."
            )
            return
        raise ProjectConfigError(
            f"Database does not exist: {db}; select the existing file with --db"
        )
    if find_server(db):
        raise ProjectConfigError(
            "Stop the server and disconnect auto-starting clients before offline relocation"
        )
    ops = plan_files(db, old, new)
    # Read-only opens neither stamp metadata nor checkpoint/change permissions.
    # The native file lock also refuses an unregistered writer.
    with Engine(config, read_only=dry_run) as engine, engine.serialized_writes():
        edits = plan_graph(engine, old, new)
        if dry_run:
            for edit in edits:
                print(edit.description)
            preview_ops(list(ops))
            print(
                f"Preview: {len(edits)} graph updates; node IDs and relationships retained."
            )
            return
        with engine.write_transaction():
            for edit in edits:
                engine.execute_write(edit.query, edit.params)
    try:
        apply_ops(list(ops))
    except (ProjectConfigError, OSError) as exc:
        raise ProjectConfigError(
            f"Graph paths committed, but client-file publication failed: {exc}. "
            "Keep the init backups and rerun the same relocation to finish configuration."
        ) from exc
    print(
        f"Relocated {len(edits)} graph path/settings groups. IDs, memories, and relationships preserved."
    )
    print(
        "Restart clients; code freshness must verify the new paths. For user-scope registrations or linked skills, rerun init with the intended client."
    )
