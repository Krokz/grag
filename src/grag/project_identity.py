"""Local checkout identity and one database-selection policy for the CLI.

The manifest travels with a checkout but is deliberately untracked. A Git
worktree is a separate checkout; an explicit --db is how checkouts share memory.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from grag.config import derive_port
from grag.config_document import read_server
from grag.project_files import ProjectConfigError, WriteOp, snapshot

MANIFEST = Path(".grag/project.json")


@dataclass(frozen=True)
class ProjectIdentity:
    version: int
    checkout_id: str
    root: str
    db_path: str
    port: int


def project_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    legacy_root = None
    for directory in (start, *start.parents):
        if (
            (directory / MANIFEST).exists()
            or (directory / MANIFEST).is_symlink()
            or (directory / ".git").exists()
        ):
            return directory
        if legacy_root is None and any(
            (directory / name).exists()
            for name in (".mcp.json", ".cursor/mcp.json", "knowledge.lbdb")
        ):
            legacy_root = directory
    return legacy_root or start


def read_identity(root: Path, *, allow_moved: bool = False) -> ProjectIdentity | None:
    path = root / MANIFEST
    before = snapshot(path)
    if before.data is None:
        return None
    try:
        # Strict JSON also rejects duplicate keys; a manifest has a fixed schema.
        from grag.config_document import read_object

        value = read_object(before.text)
        identity = ProjectIdentity(**value)
        if type(identity.version) is not int or identity.version != 1:
            raise ValueError("unsupported manifest version")
        UUID(identity.checkout_id)
        if not all(
            isinstance(p, str) and Path(p).is_absolute()
            for p in (identity.root, identity.db_path)
        ):
            raise ValueError("root and db_path must be absolute paths")
        if type(identity.port) is not int or not 1 <= identity.port <= 65535:
            raise ValueError("invalid port")
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ProjectConfigError(
            f"{path}: invalid project mapping ({exc}); left intact"
        ) from exc
    if Path(identity.root) != root and not allow_moved:
        raise ProjectConfigError(
            f"Checkout moved or copied from {identity.root} to {root}. "
            "After a move, run grag relocate <old-root> <new-root>. "
            "For a copy, run grag init to give it a separate identity."
        )
    return identity


def legacy_database(root: Path) -> tuple[Path, int | None] | None:
    """Read only project-scoped registrations; never infer ownership by basename."""
    choices: dict[Path, set[int]] = {}
    for relative in (".mcp.json", ".cursor/mcp.json"):
        before = snapshot(root / relative)
        if before.data is None:
            continue
        try:
            entry = read_server(before.text, "mcpServers")
            if not entry:
                continue
            args = registration_args(entry)
            if any(a == "--server-url" or a.startswith("--server-url=") for a in args):
                continue
            if "--db" not in args:
                raise ValueError(
                    "grag registration has no explicit database path; select --db and rerun init"
                )
            raw = args[args.index("--db") + 1]
            if not Path(raw).is_absolute():
                raise ValueError(
                    "relative legacy --db is ambiguous; select --db explicitly"
                )
            db = Path(raw).resolve()
            ports = choices.setdefault(db, set())
            if "--port" in args:
                port = int(args[args.index("--port") + 1])
                if not 1 <= port <= 65535:
                    raise ValueError("invalid grag port")
                ports.add(port)
        except (ValueError, IndexError, RecursionError) as exc:
            raise ProjectConfigError(f"{root / relative}: {exc}") from exc
    if len(choices) > 1 or any(len(ports) > 1 for ports in choices.values()):
        raise ProjectConfigError(
            "Project grag registrations disagree; select --db explicitly and rerun init"
        )
    if choices:
        db, ports = next(iter(choices.items()))
        return db, next(iter(ports), None)
    return None


def registration_args(entry: dict) -> list[str]:
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("grag args must be a string array")
    normalized: list[str] = []
    for arg in args:
        normalized.extend(
            arg.split("=", 1) if arg.startswith(("--db=", "--port=")) else [arg]
        )
    for flag in ("--db", "--port"):
        if normalized.count(flag) > 1:
            raise ValueError(
                f"duplicate {flag} in grag registration; select the target explicitly"
            )
    return normalized


def explicit_database(args: object) -> bool:
    return bool(
        getattr(args, "db", None)
        or getattr(args, "db_dir", None)
        or os.getenv("GRAG_DB_PATH")
        or os.getenv("GRAG_DB_DIR")
    )


def identity_ops(root: Path, identity: ProjectIdentity) -> list[WriteOp]:
    path = root / MANIFEST
    ops = [WriteOp(path, json.dumps(asdict(identity), indent=2) + "\n", snapshot(path))]
    ignore = path.parent / ".gitignore"
    before = snapshot(ignore)
    content = before.text if before.data is not None else ""
    if not content.rstrip().endswith("/project.json"):
        ops.append(
            WriteOp(
                ignore,
                content
                + ("\n" if content and not content.endswith("\n") else "")
                + "# Local checkout identity and database path.\n/project.json\n",
                before,
            )
        )
    return ops


def new_identity(
    root: Path, db: Path | None = None, port: int | None = None
) -> ProjectIdentity:
    checkout_id = str(uuid4())
    name = root.name.encode("utf-8")[:120].decode("utf-8", errors="ignore") or "project"
    db = db or (Path.home() / ".grag" / f"{name}-{checkout_id}.lbdb")
    db = db.expanduser().resolve()
    return ProjectIdentity(1, checkout_id, str(root), str(db), port or derive_port(db))


def rebase(value: str, old: Path, new: Path) -> str:
    """Only absolute path components, never prose, URLs, or prefix lookalikes."""
    path = Path(value)
    if not path.is_absolute():
        return value
    try:
        return str(new / path.relative_to(old))
    except ValueError:
        return value
