"""Tests for grag.project (grag init logic)."""

from __future__ import annotations

import json
import sys
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


def test_detect_clients_fallback_to_claude(tmp_path, monkeypatch):
    """No client dirs → default to claude so .mcp.json is always written."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    clients = detect_clients(tmp_path)
    assert clients == ["claude"]


def test_detect_clients_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    (tmp_path / ".cursor").mkdir()
    assert "cursor" in detect_clients(tmp_path)


def test_detect_clients_cursor_from_home_dir(tmp_path, monkeypatch):
    """A user-level ~/.cursor means the user runs Cursor — configure it even in
    a fresh project that has no .cursor dir yet."""
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert "cursor" in detect_clients(tmp_path / "project")


def test_detect_clients_does_not_duplicate(tmp_path, monkeypatch):
    """Each client appears at most once."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    (tmp_path / ".cursor").mkdir()
    clients = detect_clients(tmp_path)
    assert clients.count("cursor") == 1


# ---------------------------------------------------------------------------
# plan_mcp_ops — claude (.mcp.json)
# ---------------------------------------------------------------------------


def test_claude_op_creates_mcp_json_stdio(tmp_path):
    """Default transport is stdio+auto-serve (no lock conflict, LLM can start grag)."""
    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, WriteOp)
    assert op.path == tmp_path / ".mcp.json"
    assert op.created
    data = json.loads(op.content)
    entry = data["mcpServers"]["grag"]
    assert "command" in entry
    args = entry["args"]
    assert str(db.resolve()) in args
    assert "mcp" in args
    assert "--auto-serve" in args


def test_claude_op_creates_mcp_json_url(tmp_path):
    """stdio=False writes the URL entry (requires manual server start)."""
    db = tmp_path / "test.lbdb"
    ops = plan_mcp_ops(["claude"], tmp_path, db, stdio=False)
    assert len(ops) == 1
    data = json.loads(ops[0].content)
    entry = data["mcpServers"]["grag"]
    assert "url" in entry
    assert "127.0.0.1:8471" in entry["url"]
    assert "/mcp/" in entry["url"]


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
    """Old URL entry is replaced with stdio+auto-serve entry (default transport)."""
    db = tmp_path / "new.lbdb"
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"grag": {"url": "http://old:1234/mcp/"}}})
    )
    ops = plan_mcp_ops(["claude"], tmp_path, db)
    data = json.loads(ops[0].content)
    entry = data["mcpServers"]["grag"]
    assert "command" in entry
    assert "--auto-serve" in entry["args"]
    assert "url" not in entry  # old URL entry fully replaced


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


# ---------------------------------------------------------------------------
# embed env + binary path
# ---------------------------------------------------------------------------


def test_stdio_entry_bakes_embed_env_when_fastembed_installed(tmp_path, monkeypatch):
    import grag.project as project

    monkeypatch.setattr(project, "_fastembed_available", lambda: True)
    ops = plan_mcp_ops(["claude"], tmp_path, tmp_path / "kb.lbdb")
    entry = json.loads(ops[0].content)["mcpServers"]["grag"]
    assert entry["env"] == {"GRAG_EMBED_PROVIDER": "fastembed"}


def test_stdio_entry_omits_embed_env_without_fastembed(tmp_path, monkeypatch):
    import grag.project as project

    monkeypatch.setattr(project, "_fastembed_available", lambda: False)
    ops = plan_mcp_ops(["claude"], tmp_path, tmp_path / "kb.lbdb")
    entry = json.loads(ops[0].content)["mcpServers"]["grag"]
    assert "env" not in entry


def test_grag_bin_prefers_bare_name_for_global_install(monkeypatch):
    import grag.project as project

    # Global install: which() finds grag outside sys.prefix -> bare "grag".
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/grag" if name == "grag" else None
    )
    assert project._grag_bin() == "grag"


def test_grag_bin_keeps_absolute_path_for_venv_install(monkeypatch, tmp_path):
    import grag.project as project

    venv_bin = tmp_path / "venv" / "bin" / "grag"
    venv_bin.parent.mkdir(parents=True)
    venv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr("shutil.which", lambda name: str(venv_bin))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert project._grag_bin() == str(venv_bin)


# ---------------------------------------------------------------------------
# plan_remove_ops (grag init --remove)
# ---------------------------------------------------------------------------


def test_remove_ops_strip_grag_entry_but_keep_others(tmp_path):
    from grag.project import plan_remove_ops

    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {"mcpServers": {"grag": {"command": "grag"}, "other": {"command": "x"}}}
        )
    )
    ops = plan_remove_ops(["claude"], tmp_path)
    assert len(ops) == 1
    data = json.loads(ops[0].content)
    assert "grag" not in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_remove_ops_nothing_when_no_grag_entries(tmp_path):
    from grag.project import plan_remove_ops

    assert plan_remove_ops(["claude", "cursor"], tmp_path) == []


def test_remove_ops_strip_claude_md_block(tmp_path):
    from grag.project import plan_remove_ops

    (tmp_path / "CLAUDE.md").write_text(
        f"# Project\n\n{_BLOCK_START}\ngrag stuff\n{_BLOCK_END}\n\n# Rest\n"
    )
    ops = plan_remove_ops([], tmp_path)
    assert len(ops) == 1
    content = ops[0].content
    assert _BLOCK_START not in content
    assert "# Project" in content and "# Rest" in content


def test_remove_ops_skip_jsonc_config(tmp_path):
    from grag.project import plan_remove_ops

    (tmp_path / ".mcp.json").write_text('// comment\n{"mcpServers": {"grag": {}}}')
    ops = plan_remove_ops(["claude"], tmp_path)
    assert len(ops) == 1
    assert isinstance(ops[0], SkipOp)


# ---------------------------------------------------------------------------
# SKILL.md scaffolding
# ---------------------------------------------------------------------------


def test_skill_ops_claude_and_cursor_paths(tmp_path, monkeypatch):
    from grag.project import plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    ops = plan_skill_ops(["claude", "cursor"], tmp_path)
    paths = {op.path for op in ops}
    assert paths == {
        tmp_path / ".claude" / "skills" / "grag" / "SKILL.md",
        tmp_path / ".cursor" / "skills" / "grag" / "SKILL.md",
    }
    assert all(op.created for op in ops)
    assert all(op.content.startswith("---\n") for op in ops)


def test_skill_ops_skip_clients_without_skill_support(tmp_path, monkeypatch):
    from grag.project import plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    assert plan_skill_ops(["windsurf", "zed"], tmp_path) == []


def test_skill_ops_include_agents_when_codex_present(tmp_path, monkeypatch):
    from grag.project import plan_skill_ops

    (tmp_path / ".agents").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    ops = plan_skill_ops(["claude"], tmp_path)
    assert tmp_path / ".agents" / "skills" / "grag" / "SKILL.md" in {
        op.path for op in ops
    }


def test_skill_ops_cover_detected_harnesses_beyond_clients(tmp_path, monkeypatch):
    """A project .cursor dir gets the skill even when --client was claude only."""
    from grag.project import plan_skill_ops

    (tmp_path / ".cursor").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    ops = plan_skill_ops(["claude"], tmp_path)
    assert tmp_path / ".cursor" / "skills" / "grag" / "SKILL.md" in {
        op.path for op in ops
    }


def test_skill_ops_cover_home_detected_harnesses(tmp_path, monkeypatch):
    """A user-level ~/.cursor gets the skill even in a fresh project."""
    from grag.project import plan_skill_ops

    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"
    ops = plan_skill_ops(["claude"], project)
    assert project / ".cursor" / "skills" / "grag" / "SKILL.md" in {
        op.path for op in ops
    }


def test_skill_ops_replace_existing_grag_skill(tmp_path, monkeypatch):
    """An existing grag skill (frontmatter name: grag) is upgraded in place."""
    from grag.project import _skill_template, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    existing = tmp_path / ".claude" / "skills" / "grag" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\nname: grag\ndescription: old\n---\n\nold version\n")
    ops = plan_skill_ops(["claude"], tmp_path)
    assert len(ops) == 1
    assert not ops[0].created
    assert ops[0].content == _skill_template()


def test_skill_ops_append_to_foreign_skill(tmp_path, monkeypatch):
    """A non-grag SKILL.md is user content: append, never clobber."""
    from grag.project import _skill_template, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    existing = tmp_path / ".claude" / "skills" / "grag" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\nname: mine\ndescription: mine\n---\n\nuser content\n")
    ops = plan_skill_ops(["claude"], tmp_path)
    assert len(ops) == 1
    assert not ops[0].created
    assert ops[0].content.startswith("---\nname: mine\n")
    assert "user content" in ops[0].content
    assert ops[0].content.endswith(_skill_template())


def test_skill_ops_noop_when_already_current(tmp_path, monkeypatch):
    from grag.project import apply_ops, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    assert plan_skill_ops(["claude"], tmp_path) == []


def test_remove_ops_delete_unmodified_skill(tmp_path, monkeypatch):
    from grag.project import DeleteOp, apply_ops, plan_remove_ops, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    skill = tmp_path / ".claude" / "skills" / "grag" / "SKILL.md"
    assert skill.exists()
    ops = plan_remove_ops(["claude"], tmp_path)
    assert DeleteOp(skill) in ops
    apply_ops(ops)
    assert not skill.exists()


def test_remove_ops_keep_modified_skill(tmp_path, monkeypatch):
    from grag.project import apply_ops, plan_remove_ops, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    skill = tmp_path / ".claude" / "skills" / "grag" / "SKILL.md"
    skill.write_text("user edits")
    ops = plan_remove_ops(["claude"], tmp_path)
    assert len(ops) == 1
    assert isinstance(ops[0], SkipOp)
    assert "modified" in ops[0].reason


def test_remove_ops_restore_appended_skill(tmp_path, monkeypatch):
    """--remove after an append restores the user's original file content."""
    from grag.project import apply_ops, plan_remove_ops, plan_skill_ops

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "homeless")
    skill = tmp_path / ".claude" / "skills" / "grag" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    original = "---\nname: mine\ndescription: mine\n---\n\nuser content\n"
    skill.write_text(original)
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    assert skill.read_text() != original  # template was appended
    ops = plan_remove_ops(["claude"], tmp_path)
    assert len(ops) == 1
    assert isinstance(ops[0], WriteOp)
    apply_ops(ops)
    assert skill.read_text() == original
