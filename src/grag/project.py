"""Project initialisation — register grag with LLM clients and update CLAUDE.md.

Supported clients and their MCP config locations:
    claude   — project-root/.mcp.json        (Claude Code, project-scoped)
    cursor   — project-root/.cursor/mcp.json
    windsurf — ~/.codeium/windsurf/mcp_config.json
    zed      — ~/.config/zed/settings.json   (JSONC — only if file is absent)
"""

from __future__ import annotations

import json
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
    """Absolute path to the grag binary that is currently running."""
    import shutil

    found = shutil.which("grag")
    if found:
        return found
    # grag is running right now; sys.argv[0] is definitionally correct.
    return sys.argv[0]


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _stdio_entry(db_path: Path) -> dict:
    return {"command": _grag_bin(), "args": ["--db", str(db_path.resolve()), "mcp"]}


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


# ---------------------------------------------------------------------------
# per-client MCP config
# ---------------------------------------------------------------------------


def _op_claude(project_root: Path, db_path: Path) -> WriteOp:
    path = project_root / ".mcp.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = _stdio_entry(db_path)
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_cursor(project_root: Path, db_path: Path) -> WriteOp:
    path = project_root / ".cursor" / "mcp.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = _stdio_entry(db_path)
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_windsurf(db_path: Path) -> WriteOp:
    path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    data = _load_json(path)
    data.setdefault("mcpServers", {})["grag"] = _stdio_entry(db_path)
    return WriteOp(path, _dump_json(data), not path.exists())


def _op_zed(db_path: Path) -> WriteOp | SkipOp:
    """Zed settings are JSONC; only write when the file is absent or plain JSON."""
    path = Path.home() / ".config" / "zed" / "settings.json"
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if "//" in raw or "/*" in raw:
            entry = _stdio_entry(db_path)
            snippet = json.dumps(
                {
                    "context_servers": {
                        "grag": {
                            "command": {"path": entry["command"], "args": entry["args"]}
                        }
                    }
                },
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
    entry = _stdio_entry(db_path)
    data.setdefault("context_servers", {})["grag"] = {
        "command": {"path": entry["command"], "args": entry["args"]}
    }
    return WriteOp(path, _dump_json(data), not path.exists())


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
    if (project_root / ".cursor").is_dir():
        found.append("cursor")
    if (Path.home() / ".codeium" / "windsurf").is_dir():
        found.append("windsurf")
    if (Path.home() / ".config" / "zed").is_dir():
        found.append("zed")
    return found or ["claude"]


def plan_mcp_ops(
    clients: list[str], project_root: Path, db_path: Path
) -> list[WriteOp | SkipOp]:
    ops: list[WriteOp | SkipOp] = []
    for client in clients:
        if client == "claude":
            ops.append(_op_claude(project_root, db_path))
        elif client == "cursor":
            ops.append(_op_cursor(project_root, db_path))
        elif client == "windsurf":
            ops.append(_op_windsurf(db_path))
        elif client == "zed":
            ops.append(_op_zed(db_path))
    return ops


# ---------------------------------------------------------------------------
# CLAUDE.md block
# ---------------------------------------------------------------------------


def _claude_md_block(db_path: Path) -> str:
    db = db_path.resolve()
    return (
        f"{_BLOCK_START}\n"
        "## grag\n\n"
        f"Database: `{db}`  \n"
        f"Run: `grag --db {db} serve --with-mcp`\n\n"
        "**Always call `search_knowledge` before answering questions about this project.**  \n"
        "Unfamiliar code: `ingest_code` first. New facts: `upsert_nodes` / `upsert_edges`.\n"
        f"{_BLOCK_END}"
    )


def plan_claude_md_op(project_root: Path, db_path: Path) -> WriteOp:
    """Build a write-op that inserts or replaces the grag block in CLAUDE.md."""
    path = project_root / "CLAUDE.md"
    block = _claude_md_block(db_path)
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


def apply_ops(ops: list[WriteOp | SkipOp]) -> None:
    """Write files and report; print manual instructions for SkipOps."""
    for op in ops:
        if isinstance(op, SkipOp):
            print(f"  skip:   {op.path}  ({op.reason})")
            if op.snippet:
                print(f"          Add this to {op.path} manually:\n")
                for line in op.snippet.splitlines():
                    print(f"          {line}")
                print()
        else:
            verb = "create" if op.created else "update"
            print(f"  {verb}: {op.path}")
            op.path.parent.mkdir(parents=True, exist_ok=True)
            op.path.write_text(op.content, encoding="utf-8")
