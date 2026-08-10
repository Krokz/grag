"""Tests for grag.project (grag init logic)."""

from __future__ import annotations

import json
from pathlib import Path

from grag.project import (
    _BLOCK_END,
    _BLOCK_START,
    SkipOp,
    WriteOp,
    detect_clients,
    plan_claude_md_op,
    plan_mcp_ops,
)

# ---------------------------------------------------------------------------
# detect_clients
# ---------------------------------------------------------------------------


def test_detect_clients_fallback_to_claude(tmp_path):
    """No client dirs → default to claude so .mcp.json is always written."""
    clients = detect_clients(tmp_path)
    assert clients == ["claude"]


def test_detect_clients_cursor(tmp_path):
    (tmp_path / ".cursor").mkdir()
    assert "cursor" in detect_clients(tmp_path)


def test_detect_clients_does_not_duplicate(tmp_path):
    """Each client appears at most once."""
    (tmp_path / ".cursor").mkdir()
    clients = detect_clients(tmp_path)
    assert clients.count("cursor") == 1


# ---------------------------------------------------------------------------
# plan_mcp_ops — claude (.mcp.json)
# ---------------------------------------------------------------------------


def test_claude_op_creates_mcp_json(tmp_path):
    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, WriteOp)
    assert op.path == tmp_path / ".mcp.json"
    assert op.created
    data = json.loads(op.content)
    entry = data["mcpServers"]["grag"]
    assert str(db.resolve()) in entry["args"]
    assert "mcp" in entry["args"]


def test_claude_op_merges_with_existing(tmp_path):
    db = tmp_path / "test.lbdb"
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    data = json.loads(ops[0].content)
    assert "other" in data["mcpServers"]
    assert "grag" in data["mcpServers"]
    assert not ops[0].created


def test_claude_op_overwrites_existing_grag_entry(tmp_path):
    db = tmp_path / "new.lbdb"
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"grag": {"command": "old", "args": []}}})
    )
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    data = json.loads(ops[0].content)
    assert str(db.resolve()) in data["mcpServers"]["grag"]["args"]


# ---------------------------------------------------------------------------
# plan_mcp_ops — cursor (.cursor/mcp.json)
# ---------------------------------------------------------------------------


def test_cursor_op_path(tmp_path):
    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["cursor"], tmp_path, db)
    assert ops[0].path == tmp_path / ".cursor" / "mcp.json"


# ---------------------------------------------------------------------------
# plan_mcp_ops — zed (skips JSONC files)
# ---------------------------------------------------------------------------


def test_zed_op_skips_jsonc(tmp_path, monkeypatch):
    zed_dir = tmp_path / ".config" / "zed"
    zed_dir.mkdir(parents=True)
    settings = zed_dir / "settings.json"
    settings.write_text("// zed settings\n{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["zed"], tmp_path, db)
    assert len(ops) == 1
    assert isinstance(ops[0], SkipOp)
    assert "JSONC" in ops[0].reason or "comments" in ops[0].reason.lower()
    assert "context_servers" in ops[0].snippet


def test_zed_op_writes_when_plain_json(tmp_path, monkeypatch):
    zed_dir = tmp_path / ".config" / "zed"
    zed_dir.mkdir(parents=True)
    (zed_dir / "settings.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["zed"], tmp_path, db)
    assert isinstance(ops[0], WriteOp)
    data = json.loads(ops[0].content)
    assert "context_servers" in data


def test_zed_op_creates_when_absent(tmp_path, monkeypatch):
    zed_dir = tmp_path / ".config" / "zed"
    zed_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["zed"], tmp_path, db)
    assert isinstance(ops[0], WriteOp)
    assert ops[0].created


# ---------------------------------------------------------------------------
# plan_claude_md_op
# ---------------------------------------------------------------------------


def test_claude_md_creates_when_absent(tmp_path):
    op = plan_claude_md_op(tmp_path, tmp_path / "test.lbdb")
    assert op.created
    assert _BLOCK_START in op.content
    assert _BLOCK_END in op.content
    assert str((tmp_path / "test.lbdb").resolve()) in op.content


def test_claude_md_appends_to_existing(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Existing\n\nSome content.\n")
    op = plan_claude_md_op(tmp_path, tmp_path / "test.lbdb")
    assert not op.created
    assert "Existing" in op.content
    assert _BLOCK_START in op.content
    assert _BLOCK_END in op.content


def test_claude_md_replaces_existing_block(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        f"# Existing\n\n{_BLOCK_START}\nOLD CONTENT\n{_BLOCK_END}\n\n# After\n"
    )
    op = plan_claude_md_op(tmp_path, tmp_path / "test.lbdb")
    assert "OLD CONTENT" not in op.content
    assert "# After" in op.content
    assert _BLOCK_START in op.content


def test_claude_md_block_appears_once(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(f"{_BLOCK_START}\nOLD\n{_BLOCK_END}\n")
    op = plan_claude_md_op(tmp_path, tmp_path / "test.lbdb")
    assert op.content.count(_BLOCK_START) == 1
    assert op.content.count(_BLOCK_END) == 1


# ---------------------------------------------------------------------------
# apply_ops
# ---------------------------------------------------------------------------


def test_apply_ops_creates_file(tmp_path):
    from grag.project import apply_ops

    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    apply_ops(ops)
    written = tmp_path / ".mcp.json"
    assert written.exists()
    data = json.loads(written.read_text())
    assert "grag" in data["mcpServers"]


def test_apply_ops_creates_parent_dirs(tmp_path):
    from grag.project import apply_ops

    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["cursor"], tmp_path, db)
    apply_ops(ops)
    assert (tmp_path / ".cursor" / "mcp.json").exists()
