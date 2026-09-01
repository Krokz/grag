"""Project initialisation — register grag with LLM clients and update CLAUDE.md.

Supported clients and their MCP config locations:
    claude   — project-root/.mcp.json        (Claude Code, project-scoped)
    cursor   — project-root/.cursor/mcp.json
    windsurf — ~/.codeium/windsurf/mcp_config.json
    zed      — ~/.config/zed/settings.json   (JSONC — only if file is absent)

Skill-capable harnesses also get the packaged grag SKILL.md (the operating
guidance that makes the MCP tools effective):
    claude   — project-root/.claude/skills/grag/SKILL.md
    cursor   — project-root/.cursor/skills/grag/SKILL.md
    codex    — project-root/.agents/skills/grag/SKILL.md  (detected, no MCP config)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

# Idempotency markers so we can detect and replace an existing block.
_BLOCK_START = "<!-- grag:start -->"
_BLOCK_END = "<!-- grag:end -->"

_CLIENTS: tuple[str, ...] = ("claude", "cursor", "windsurf", "zed")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _grag_bin() -> str:
    """Command the MCP client should spawn for grag.

    A globally installed grag (pipx / uv tool / homebrew — anything outside the
    running interpreter's prefix) is written as bare ``"grag"``: it stays valid
    when the install is upgraded or moved. A grag inside the current virtualenv
    keeps its absolute path — the client's PATH won't have the venv on it —
    at the cost of breaking if the venv is recreated (re-run ``grag init``).
    """
    import shutil

    found = shutil.which("grag")
    if found:
        if not Path(found).resolve().is_relative_to(Path(sys.prefix).resolve()):
            return "grag"
        return found
    # grag is running right now; sys.argv[0] is definitionally correct.
    return sys.argv[0]


def _fastembed_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("fastembed") is not None
    except (ImportError, ValueError):
        return False


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _stdio_entry(db_path: Path, port: int = 8471) -> dict:
    """Stdio entry with --auto-serve: starts grag serve --with-mcp if needed, then proxies.

    The proxy process holds no write lock, so the browser UI and LLM tools work
    simultaneously. When fastembed is installed, GRAG_EMBED_PROVIDER is baked
    into the entry so the auto-served daemon inherits it — otherwise a user who
    installed the embed-local extra would silently get FTS-only retrieval.
    """
    entry: dict = {
        "command": _grag_bin(),
        "args": [
            "--db",
            str(db_path.resolve()),
            "mcp",
            "--auto-serve",
            "--port",
            str(port),
        ],
    }
    if _fastembed_available():
        entry["env"] = {"GRAG_EMBED_PROVIDER": "fastembed"}
    return entry


def _url_entry(port: int) -> dict:
    return {"url": f"http://127.0.0.1:{port}/mcp/"}


# ---------------------------------------------------------------------------
# write-op plan
# ---------------------------------------------------------------------------


class WriteOp(NamedTuple):
    path: Path
    content: str
    created: bool


class SkipOp(NamedTuple):
    """A file we decided not to write, with a reason and an optional snippet."""

    path: Path
    reason: str
    snippet: str = ""


class DeleteOp(NamedTuple):
    """A file init created that --remove should delete outright."""

    path: Path


# ---------------------------------------------------------------------------
# per-client MCP config
# ---------------------------------------------------------------------------


def _op_claude(project_root: Path, db_path: Path, stdio: bool, port: int) -> WriteOp:
    path = project_root / ".mcp.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = (
        _stdio_entry(db_path, port) if stdio else _url_entry(port)
    )
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_cursor(project_root: Path, db_path: Path, stdio: bool, port: int) -> WriteOp:
    path = project_root / ".cursor" / "mcp.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = (
        _stdio_entry(db_path, port) if stdio else _url_entry(port)
    )
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_windsurf(db_path: Path, stdio: bool, port: int) -> WriteOp:
    path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = (
        _stdio_entry(db_path, port) if stdio else _url_entry(port)
    )
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_zed(db_path: Path, port: int = 8471) -> WriteOp | SkipOp:
    """Zed context_servers only support stdio; always writes stdio regardless of transport mode.

    Also skips when settings.json contains JSONC comments — merge would lose them.
    """
    path = Path.home() / ".config" / "zed" / "settings.json"
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if "//" in raw or "/*" in raw:
            entry = _stdio_entry(db_path, port)
            snippet = json.dumps(
                {"context_servers": {"grag": _zed_command(entry)}},
                indent=2,
            )
            return SkipOp(
                path,
                "settings.json contains comments (JSONC) — merge would lose them",
                snippet,
            )
        data = _load_json(path)
    else:
        data = {}
    entry = _stdio_entry(db_path, port)
    data.setdefault("context_servers", {})["grag"] = _zed_command(entry)
    return WriteOp(path, _dump_json(data), not path.exists())


def _zed_command(entry: dict) -> dict:
    command = {"path": entry["command"], "args": entry["args"]}
    if "env" in entry:
        command["env"] = entry["env"]
    return {"command": command}


# ---------------------------------------------------------------------------
# client detection + plan_mcp_ops
# ---------------------------------------------------------------------------


def detect_clients(project_root: Path) -> list[str]:
    """Return the names of LLM clients whose config dirs are present.

    Falls back to ['claude'] (writes .mcp.json) when nothing is detected —
    .mcp.json is the most widely understood format and is harmless to create.
    """
    found: list[str] = []
    if (project_root / ".claude").is_dir() or (Path.home() / ".claude").is_dir():
        found.append("claude")
    if (project_root / ".cursor").is_dir() or (Path.home() / ".cursor").is_dir():
        found.append("cursor")
    if (Path.home() / ".codeium" / "windsurf").is_dir():
        found.append("windsurf")
    if (Path.home() / ".config" / "zed").is_dir():
        found.append("zed")
    return found or ["claude"]


def plan_mcp_ops(
    clients: list[str],
    project_root: Path,
    db_path: Path,
    *,
    stdio: bool = True,
    port: int = 8471,
) -> list[WriteOp | SkipOp]:
    """Plan MCP config write-ops.

    Default transport is stdio with ``--auto-serve``: the client starts
    ``grag mcp --auto-serve`` which auto-starts ``grag serve --with-mcp``
    as a background daemon if it is not running, then proxies the stdio
    connection to it.  The proxy holds no write lock, so the browser UI and
    LLM tools work simultaneously.

    Pass ``stdio=False`` for explicit URL transport: writes ``{"url": "http://..."}``
    and requires the user to start ``grag serve --with-mcp`` manually before
    connecting.
    """
    ops: list[WriteOp | SkipOp] = []
    for client in clients:
        if client == "claude":
            ops.append(_op_claude(project_root, db_path, stdio, port))
        elif client == "cursor":
            ops.append(_op_cursor(project_root, db_path, stdio, port))
        elif client == "windsurf":
            ops.append(_op_windsurf(db_path, stdio, port))
        elif client == "zed":
            ops.append(_op_zed(db_path, port))  # Zed context_servers are stdio-only
    return ops


# ---------------------------------------------------------------------------
# SKILL.md scaffolding
# ---------------------------------------------------------------------------

# Harnesses that read a project-level SKILL.md, keyed by init client.
# windsurf/zed have no skill mechanism.
_SKILL_DIRS = {"claude": ".claude", "cursor": ".cursor"}


def _skill_template() -> str:
    """The canonical grag SKILL.md shipped inside the wheel."""
    from importlib.resources import files

    return files("grag.assets").joinpath("skill/SKILL.md").read_text(encoding="utf-8")


def _skill_paths(clients: list[str], project_root: Path) -> list[Path]:
    """Project-level SKILL.md targets for every harness the user evidently uses.

    A skill file is inert unless its harness reads it, so scaffold beyond the
    --client list: any harness with a project-level or user-level config dir
    gets one. Codex (``.agents/skills/``) has no MCP config in ``grag init``
    but reads the same skill format.
    """
    dirs = [_SKILL_DIRS[c] for c in clients if c in _SKILL_DIRS]
    if (project_root / ".claude").is_dir() or (Path.home() / ".claude").is_dir():
        dirs.append(".claude")
    if (project_root / ".cursor").is_dir() or (Path.home() / ".cursor").is_dir():
        dirs.append(".cursor")
    if (project_root / ".agents").is_dir() or (Path.home() / ".codex").is_dir():
        dirs.append(".agents")
    return [
        project_root / d / "skills" / "grag" / "SKILL.md"
        for d in dict.fromkeys(dirs)
    ]


def _is_grag_skill(text: str) -> bool:
    """Whether an existing SKILL.md is grag's own (frontmatter ``name: grag``)."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return False
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip() == "grag"
    return False


def plan_skill_ops(clients: list[str], project_root: Path) -> list[WriteOp]:
    """Plan SKILL.md writes so agents get grag's operating guidance, not just tools.

    An existing file is only replaced when it is grag's own skill (an older
    template or a user-edited grag skill — frontmatter ``name: grag``). Any
    other SKILL.md is user content: the template is appended, never clobbered.
    """
    template = _skill_template()
    ops: list[WriteOp] = []
    for path in _skill_paths(clients, project_root):
        if not path.exists():
            ops.append(WriteOp(path, template, True))
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if current == template:
            continue  # already current — nothing to do
        if _is_grag_skill(current):
            ops.append(WriteOp(path, template, False))
        else:
            ops.append(
                WriteOp(path, current.rstrip("\n") + "\n\n" + template, False)
            )
    return ops


def plan_skill_removal_ops(
    clients: list[str], project_root: Path
) -> list[DeleteOp | SkipOp | WriteOp]:
    """Plan SKILL.md removals.

    A file holding only the template is deleted; a file the template was
    appended to is restored to its pre-init content; anything else was
    modified by the user and is skipped with a manual-removal note.
    """
    template = _skill_template()
    ops: list[DeleteOp | SkipOp | WriteOp] = []
    for path in _skill_paths(clients, project_root):
        if not path.is_file():
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if current == template:
            ops.append(DeleteOp(path))
        elif current.endswith(template):
            prefix = current[: -len(template)].rstrip("\n")
            ops.append(WriteOp(path, prefix + "\n", False))
        else:
            ops.append(
                SkipOp(path, "SKILL.md was modified after init — remove it manually")
            )
    return ops


# ---------------------------------------------------------------------------
# CLAUDE.md block
# ---------------------------------------------------------------------------


def _claude_md_block(db_path: Path, port: int = 8471) -> str:
    db = db_path.resolve()
    return (
        f"{_BLOCK_START}\n"
        "## grag\n\n"
        f"Database: `{db}`  \n"
        f"Run: `GRAG_EMBED_PROVIDER=fastembed grag --db {db} serve --with-mcp --port {port}`\n\n"
        "**Always call `search_knowledge` before answering questions about this project.**  \n"
        "Unfamiliar code: `ingest_code` first. New facts: `upsert_nodes` / `upsert_edges`.\n"
        f"{_BLOCK_END}"
    )


def plan_claude_md_op(project_root: Path, db_path: Path, port: int = 8471) -> WriteOp:
    """Build a write-op that inserts or replaces the grag block in CLAUDE.md."""
    path = project_root / "CLAUDE.md"
    block = _claude_md_block(db_path, port)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        start = text.find(_BLOCK_START)
        end = text.find(_BLOCK_END)
        if start != -1 and end != -1:
            new_text = text[:start] + block + text[end + len(_BLOCK_END) :]
        else:
            sep = "\n" if text.endswith("\n") else "\n\n"
            new_text = text + sep + block + "\n"
        return WriteOp(path, new_text, False)
    return WriteOp(path, block + "\n", True)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply_ops(ops: list[WriteOp | SkipOp | DeleteOp]) -> None:
    """Write files and report; print manual instructions for SkipOps."""
    for op in ops:
        if isinstance(op, SkipOp):
            print(f"  skip:   {op.path}  ({op.reason})")
            if op.snippet:
                print(f"          Add this to {op.path} manually:\n")
                for line in op.snippet.splitlines():
                    print(f"          {line}")
                print()
        elif isinstance(op, DeleteOp):
            print(f"  delete: {op.path}")
            op.path.unlink(missing_ok=True)
        else:
            verb = "create" if op.created else "update"
            print(f"  {verb}: {op.path}")
            op.path.parent.mkdir(parents=True, exist_ok=True)
            op.path.write_text(op.content, encoding="utf-8")


# ---------------------------------------------------------------------------
# removal (grag init --remove)
# ---------------------------------------------------------------------------


def _remove_json_entry(path: Path, section: str) -> WriteOp | SkipOp | None:
    """Drop the 'grag' entry from `section` of a JSON config file.

    Returns None when the file doesn't exist or holds no grag entry.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if "//" in raw or "/*" in raw:
        return SkipOp(
            path, "file contains comments (JSONC) — remove the grag entry manually"
        )
    try:
        data = json.loads(raw)
    except ValueError:
        return SkipOp(path, "file is not valid JSON — remove the grag entry manually")
    servers = data.get(section)
    if not isinstance(servers, dict) or "grag" not in servers:
        return None
    del servers["grag"]
    return WriteOp(path, _dump_json(data), False)


def plan_remove_ops(
    clients: list[str], project_root: Path
) -> list[WriteOp | SkipOp | DeleteOp]:
    """Plan the write-ops that undo `grag init` for the given clients."""
    ops: list[WriteOp | SkipOp | DeleteOp | None] = []
    if "claude" in clients:
        ops.append(_remove_json_entry(project_root / ".mcp.json", "mcpServers"))
    if "cursor" in clients:
        ops.append(
            _remove_json_entry(project_root / ".cursor" / "mcp.json", "mcpServers")
        )
    if "windsurf" in clients:
        ops.append(
            _remove_json_entry(
                Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
                "mcpServers",
            )
        )
    if "zed" in clients:
        ops.append(
            _remove_json_entry(
                Path.home() / ".config" / "zed" / "settings.json", "context_servers"
            )
        )
    ops.append(plan_claude_md_removal(project_root))
    return [op for op in ops if op is not None] + plan_skill_removal_ops(
        clients, project_root
    )


def plan_claude_md_removal(project_root: Path) -> WriteOp | None:
    """Strip the grag block from CLAUDE.md; None when there is nothing to strip."""
    path = project_root / "CLAUDE.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    start = text.find(_BLOCK_START)
    end = text.find(_BLOCK_END)
    if start == -1 or end == -1:
        return None
    new_text = text[:start] + text[end + len(_BLOCK_END) :]
    new_text = new_text.strip("\n")
    new_text = new_text + "\n" if new_text else ""
    return WriteOp(path, new_text, False)
