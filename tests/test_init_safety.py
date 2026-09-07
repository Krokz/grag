"""M04: config preservation, review, fault injection and concurrent init plans."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from grag import cli, project_files
from grag.config_document import edit_server
from grag.project import (
    _BLOCK_END,
    _BLOCK_START,
    apply_ops,
    plan_claude_md_op,
    plan_claude_md_removal,
    plan_mcp_ops,
    plan_remove_ops,
    plan_skill_ops,
    preview_ops,
)
from grag.project_files import DeleteOp, ProjectConfigError, WriteOp, snapshot


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("grag.project._fastembed_available", lambda: False)
    monkeypatch.chdir(tmp_path)


INVALID_CONFIGS = [
    "",
    "{broken",
    "[]",
    "null",
    '"text"',
    "42",
    '{"mcpServers": null}',
    '{"mcpServers": []}',
    '{"mcpServers": "old"}',
    '{"mcpServers": {"grag": null}}',
    '{"mcpServers": {"grag": []}}',
    '{"mcpServers": {}, "mcpServers": {}}',
    '{"other": {"key": 1, "key": 2}}',
    '{"number": NaN}',
    '{"number": Infinity}',
    '{"number": -Infinity}',
    '{"mcpServers": {},}',
    '// comment\n{"mcpServers": {}}',
]


@pytest.mark.parametrize("raw", INVALID_CONFIGS)
@pytest.mark.parametrize("remove", [False, True])
def test_invalid_config_fails_before_any_writes(tmp_path, capsys, raw, remove):
    path = tmp_path / ".mcp.json"
    path.write_text(raw)
    args = ["init", "--client", "claude"] + (["--remove"] if remove else [])
    assert cli.main(args) == 1
    assert path.read_text() == raw
    assert set(tmp_path.iterdir()) == {path}
    output = capsys.readouterr()
    assert str(path) in output.err
    assert "Done" not in output.out


@pytest.mark.parametrize(
    "target", [".mcp.json", "CLAUDE.md", ".claude/skills/grag/SKILL.md"]
)
@pytest.mark.parametrize(
    "failure", ["utf8", "permission", "directory", "symlink", "dangling"]
)
def test_unreadable_and_nonregular_files_remain_intact(
    tmp_path, monkeypatch, target, failure
):
    path = tmp_path / target
    path.parent.mkdir(parents=True, exist_ok=True)
    if failure == "directory":
        path.mkdir()
    elif failure in ("symlink", "dangling"):
        other = tmp_path / "original"
        if failure == "symlink":
            other.write_text("{}")
        path.symlink_to(other)
    else:
        path.write_bytes(b"\xff" if failure == "utf8" else b"{}")
    original_open = os.open
    if failure == "permission":

        def deny(file, *args, **kwargs):
            if Path(file) == path:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(file, *args, **kwargs)

        monkeypatch.setattr(os, "open", deny)
    assert cli.main(["init", "--client", "claude"]) == 1
    assert not (Path.home() / ".grag").exists()
    if failure == "directory":
        assert path.is_dir()
    elif failure in ("symlink", "dangling"):
        assert path.is_symlink()
        if failure == "symlink":
            assert other.read_text() == "{}"
    else:
        assert path.read_bytes() == (b"\xff" if failure == "utf8" else b"{}")


@pytest.mark.parametrize(
    "text",
    [
        _BLOCK_START,
        _BLOCK_END,
        _BLOCK_END + _BLOCK_START,
        _BLOCK_START + _BLOCK_START + _BLOCK_END,
        _BLOCK_START + _BLOCK_END + _BLOCK_END,
    ],
)
def test_ambiguous_instruction_markers_abort_install_and_removal(tmp_path, text):
    path = tmp_path / "CLAUDE.md"
    path.write_text(text)
    with pytest.raises(ProjectConfigError, match="markers"):
        plan_claude_md_op(tmp_path, tmp_path / "graph.lbdb")
    with pytest.raises(ProjectConfigError, match="markers"):
        plan_claude_md_removal(tmp_path)
    assert path.read_text() == text


@pytest.mark.parametrize("indent", [None, 2, "\t"])
@pytest.mark.parametrize("position", [0, 1, 2, 3])
@pytest.mark.parametrize("jsonc", [False, True])
def test_server_edits_preserve_other_values_and_comments(indent, position, jsonc):
    # Grag first/middle/last/absent, inline/indented, and nested comment-like strings.
    pairs = [
        ("alpha", {"url": "https://other/mcp/"}),
        ("beta", {"env": {"ODD": '"// /*\\'}}),
    ]
    if position < 3:
        pairs.insert(position, ("grag", {"command": "old", "args": [1, {"x": [2, 3]}]}))
    servers = dict(pairs)
    data = {"theme": "custom", "context_servers": servers, "unrelated": [1, True, None]}
    raw = json.dumps(data, indent=indent)
    if jsonc:
        raw = "\ufeff// preserve header\r\n" + raw.replace(
            '"context_servers":', '/* keep */ "context_servers":'
        )
        # A trailing comma at the root, plus an end-of-file comment.
        raw = raw[:-1] + ",\n}// preserve footer\n"
    entry = {"command": {"path": "grag", "args": ["mcp"]}}
    written = edit_server(raw, "context_servers", entry, jsonc=jsonc)
    assert '"theme": "custom"' in written
    assert '"unrelated":' in written
    if jsonc:
        for comment in ["// preserve header", "/* keep */", "// preserve footer"]:
            assert comment in written
    assert edit_server(written, "context_servers", entry, jsonc=jsonc) == written
    removed = edit_server(written, "context_servers", None, jsonc=jsonc)
    assert '"grag"' not in removed
    if not jsonc:
        expected = {
            **data,
            "context_servers": {k: v for k, v in servers.items() if k != "grag"},
        }
        assert json.loads(removed) == expected


@pytest.mark.parametrize(
    "raw",
    [
        '{"context_servers": {/* empty */},}',
        '{"context_servers": {"other": {}, // keep other\n},}',
        '{"context_servers": {"grag": {}, /* keep this */},}',
        '{"context_servers": {"other": {}, /* between */ "grag": {}, /* tail */},}',
        '{"context_servers": {"grag": {}, /* next server */ "other": {},},}',
        '{"context_servers": {"grag": {} /* tail */},}',
        "{/* empty root */}",
    ],
)
def test_zed_trailing_commas_and_comments_survive_add_remove(raw):
    import re

    comments = re.findall(r"/\*.*?\*/|//[^\n]*", raw)
    written = edit_server(
        raw, "context_servers", {"command": {"path": "grag"}}, jsonc=True
    )
    removed = edit_server(written, "context_servers", None, jsonc=True)
    for comment in comments:
        assert comment in written and comment in removed
    assert '"grag"' not in removed
    assert edit_server(removed, "context_servers", None, jsonc=True) == removed


def test_zed_malformed_comment_does_not_get_overwritten(tmp_path):
    path = Path.home() / ".config/zed/settings.json"
    path.parent.mkdir(parents=True)
    raw = '{"context_servers": {}} /* unfinished'
    path.write_text(raw)
    assert cli.main(["init", "--client", "zed"]) == 1
    assert path.read_text() == raw


def _write_plan(path, text):
    return WriteOp(path, text, snapshot(path))


def _bundles():
    return list((Path.home() / ".grag/backups/init").glob("run-*"))


@pytest.mark.parametrize("action", ["update", "delete", "create"])
def test_stale_plan_aborts_before_writing_any_file(tmp_path, action):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("first original")
    if action != "create":
        second.write_text("second original")
    op = (
        DeleteOp(second, snapshot(second))
        if action == "delete"
        else _write_plan(second, "new")
    )
    ops = [_write_plan(first, "first new"), op]
    second.write_text("external edit")
    with pytest.raises(ProjectConfigError, match="changed since planning"):
        apply_ops(ops)
    assert first.read_text() == "first original"
    assert second.read_text() == "external edit"
    assert not _bundles()


def test_backup_exact_bytes_permissions_and_restore(tmp_path):
    path = tmp_path / ".mcp.json"
    original = (
        b'\xef\xbb\xbf{\r\n  "mcpServers": {"other": {"url": "https://other"}}\r\n}\r\n'
    )
    path.write_bytes(original)
    path.chmod(0o640)
    bundle = apply_ops(plan_mcp_ops(["claude"], tmp_path, tmp_path / "db"))
    manifest = json.loads((bundle / "manifest.json").read_text())
    entry = manifest["files"][0]
    backup = bundle / entry["backup"]
    assert backup.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == entry["before_sha256"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["after_sha256"]
    assert path.read_bytes().startswith(b"\xef\xbb\xbf{\r\n")
    assert b'"other": {"url": "https://other"}' in path.read_bytes()
    assert (bundle / "complete.json").is_file()
    if os.name != "nt":
        assert bundle.stat().st_mode & 0o777 == 0o700
        assert backup.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o640
    # Independent restore drill from the manifest, not from a retained test variable.
    restored = tmp_path / "restored.json"
    restored.write_bytes(backup.read_bytes())
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == entry["before_sha256"]
    assert json.loads(restored.read_text(encoding="utf-8-sig"))["mcpServers"] == {
        "other": {"url": "https://other"}
    }


def test_delete_backs_up_skill_and_equal_init_does_not_rewrite(tmp_path):
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    skill = tmp_path / ".claude/skills/grag/SKILL.md"
    original = skill.read_bytes()
    before = skill.stat()
    assert apply_ops(plan_skill_ops(["claude"], tmp_path)) is None
    assert skill.stat() == before
    bundle = apply_ops(plan_remove_ops(["claude"], tmp_path))
    assert not skill.exists()
    item = json.loads((bundle / "manifest.json").read_text())["files"][0]
    assert item["action"] == "delete"
    assert (bundle / item["backup"]).read_bytes() == original


@pytest.mark.parametrize(
    "phase", ["backup", "stage", "replace", "backup_fsync", "directory_fsync"]
)
def test_io_failure_never_truncates_original(tmp_path, monkeypatch, phase):
    path = tmp_path / "config"
    path.write_text("original config")
    op = _write_plan(path, "new config")
    if phase in ("backup", "stage"):
        name = "_write_file" if phase == "backup" else "_stage"

        def fail(*args, **kwargs):
            raise OSError("injected disk full")

        monkeypatch.setattr(project_files, name, fail)
    elif phase == "replace":

        def fail(*args):
            raise PermissionError("injected replace failure")

        monkeypatch.setattr(os, "replace", fail)
    elif phase == "backup_fsync":

        def fail(*args):
            raise OSError("injected sync failure")

        monkeypatch.setattr(os, "fsync", fail)
    else:
        original = project_files._sync_dir

        def fail(directory):
            if directory == tmp_path and path.read_text() == "new config":
                raise OSError("injected directory sync failure")
            return original(directory)

        monkeypatch.setattr(project_files, "_sync_dir", fail)
    with pytest.raises(ProjectConfigError, match="Published") as caught:
        apply_ops([op])
    if phase == "backup":
        assert "Incomplete backup attempt" in str(caught.value)
        assert "Preserved originals/manifest" not in str(caught.value)
    if phase == "directory_fsync":
        assert (
            path.read_text() == "new config"
        )  # rename completed; durability was not acknowledged
    else:
        assert path.read_text() == "original config"
    assert not list(tmp_path.glob(".config.grag-*"))
    assert not any((b / "complete.json").exists() for b in _bundles())
    if phase in ("stage", "replace", "directory_fsync"):
        bundle = _bundles()[0]
        assert (bundle / "000.original").read_text() == "original config"


def test_all_writes_staged_before_first_publication(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("first original")
    second.write_text("second original")
    original = project_files._stage

    def stage(op):
        if op.path == second:
            raise OSError("second staging failed")
        return original(op)

    monkeypatch.setattr(project_files, "_stage", stage)
    with pytest.raises(ProjectConfigError, match="Published 0"):
        apply_ops([_write_plan(first, "one"), _write_plan(second, "two")])
    assert first.read_text() == "first original"
    assert second.read_text() == "second original"


def test_concurrent_grag_init_is_refused(tmp_path):
    path = tmp_path / "config"
    path.write_text("original")
    with (
        project_files._init_lock(Path.home() / ".grag/backups/init"),
        pytest.raises(ProjectConfigError, match="Another grag init"),
    ):
        apply_ops([_write_plan(path, "new")])
    assert path.read_text() == "original"


def test_raced_creation_cannot_be_clobbered(tmp_path, monkeypatch):
    path = tmp_path / "config"
    original = os.link

    def create_then_link(source, destination):
        Path(destination).write_text("other process")
        original(source, destination)

    monkeypatch.setattr(os, "link", create_then_link)
    with pytest.raises(ProjectConfigError, match="Published 0"):
        apply_ops([_write_plan(path, "grag")])
    assert path.read_text() == "other process"


def test_edit_during_staging_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text("original")
    op = _write_plan(path, "grag")
    original = project_files._stage

    def stage(op):
        temp = original(op)
        op.path.write_text("editor saved")
        return temp

    monkeypatch.setattr(project_files, "_stage", stage)
    with pytest.raises(ProjectConfigError, match="changed since planning"):
        apply_ops([op])
    assert path.read_text() == "editor saved"


def test_crash_between_files_leaves_originals_and_hash_manifest(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("first original")
    second.write_text("second original")
    script = """
import os
from pathlib import Path
from grag.project_files import WriteOp, snapshot, apply_ops
paths = [Path("first"), Path("second")]
ops = [WriteOp(p, "new", snapshot(p)) for p in paths]
replace = os.replace
def crash(source, target):
    replace(source, target)
    os._exit(91)
os.replace = crash
apply_ops(ops)
"""
    env = dict(
        os.environ, PYTHONPATH=str(Path(project_files.__file__).resolve().parents[1])
    )
    result = subprocess.run(  # noqa: S603 — fixed local fault-injection script
        [sys.executable, "-c", script], env=env, check=False, timeout=20
    )
    assert result.returncode == 91
    assert first.read_text() == "new"
    assert second.read_text() == "second original"
    bundle = _bundles()[0]
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert not (bundle / "complete.json").exists()
    for entry in manifest["files"]:
        original = (bundle / entry["backup"]).read_bytes()
        assert hashlib.sha256(original).hexdigest() == entry["before_sha256"]
        assert original == f"{Path(entry['path']).name} original".encode()


@pytest.mark.parametrize("mode", ["create", "update", "delete", "unchanged"])
def test_dry_run_shows_diff_and_has_no_filesystem_effect(tmp_path, capsys, mode):
    path = tmp_path / ".mcp.json"
    if mode != "create":
        path.write_text('{"mcpServers": {"grag": {"url": "https://old"}}}\n')
    if mode == "unchanged":
        op = _write_plan(path, path.read_text())
    elif mode == "delete":
        op = DeleteOp(path, snapshot(path))
    else:
        op = plan_mcp_ops(["claude"], tmp_path, tmp_path / "db")[0]
    original = snapshot(path)
    preview_ops([op])
    output = capsys.readouterr().out
    assert snapshot(path) == original
    assert not (Path.home() / ".grag").exists()
    assert str(path) in output
    if mode == "unchanged":
        assert "unchanged" in output and "@@" not in output
    else:
        assert "@@" in output
        if mode in ("update", "delete"):
            assert '-{"mcpServers"' in output


def test_cli_dry_run_includes_actual_changes_and_never_applies(tmp_path, capsys):
    assert cli.main(["init", "--client", "claude", "--dry-run", "--no-skill"]) == 0
    output = capsys.readouterr().out
    assert '+      "command":' in output
    assert "+<!-- grag:start -->" in output
    assert not list(tmp_path.iterdir())


def test_repeated_mcp_plan_is_a_noop(tmp_path):
    apply_ops(plan_mcp_ops(["claude"], tmp_path, tmp_path / "db"))
    before = (tmp_path / ".mcp.json").stat()
    bundles = _bundles()
    assert apply_ops(plan_mcp_ops(["claude"], tmp_path, tmp_path / "db")) is None
    assert (tmp_path / ".mcp.json").stat() == before
    assert _bundles() == bundles


def test_valid_first_client_and_invalid_second_client_make_no_changes(
    tmp_path, monkeypatch
):
    first, second = tmp_path / ".mcp.json", tmp_path / ".cursor/mcp.json"
    first.write_text('{"mcpServers": {"grag": {"command": "old"}}}')
    second.parent.mkdir()
    second.write_text("{invalid}")
    originals = first.read_bytes(), second.read_bytes()
    monkeypatch.setattr(
        "grag.project.detect_clients", lambda root: ["claude", "cursor"]
    )
    assert cli.main(["init"]) == 1
    assert (first.read_bytes(), second.read_bytes()) == originals
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not _bundles()


def test_repeated_init_does_not_append_another_skill_to_foreign_content(tmp_path):
    skill = tmp_path / ".claude/skills/grag/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Custom instructions\n")
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    original = skill.read_bytes()
    assert plan_skill_ops(["claude"], tmp_path) == []
    assert skill.read_bytes() == original


def test_nonterminated_lines_have_legible_dry_run(tmp_path, capsys):
    path = tmp_path / "config"
    path.write_text("old")
    preview_ops([_write_plan(path, "new")])
    output = capsys.readouterr().out
    assert "-old\n\\ No newline at end of file\n+new\n" in output


def test_hardlinks_are_refused_without_breaking_the_shared_file(tmp_path):
    path, link = tmp_path / ".mcp.json", tmp_path / "shared"
    path.write_text("{}")
    os.link(path, link)
    with pytest.raises(ProjectConfigError, match="no links"):
        plan_mcp_ops(["claude"], tmp_path, tmp_path / "db")
    assert path.read_text() == link.read_text() == "{}"
    assert path.stat().st_ino == link.stat().st_ino


@pytest.mark.parametrize("client", ["claude", "cursor", "windsurf", "zed"])
@pytest.mark.parametrize("url", [False, True])
def test_cli_install_preview_and_remove_preserve_other_registrations(
    tmp_path, capsys, client, url
):
    paths = {
        "claude": tmp_path / ".mcp.json",
        "cursor": tmp_path / ".cursor/mcp.json",
        "windsurf": Path.home() / ".codeium/windsurf/mcp_config.json",
        "zed": Path.home() / ".config/zed/settings.json",
    }
    path = paths[client]
    path.parent.mkdir(parents=True, exist_ok=True)
    section = "context_servers" if client == "zed" else "mcpServers"
    other = '"other": {"url": "https://other/mcp/", "custom": [1, 2, 3]}'
    header = "// keep client settings\n" if client == "zed" else ""
    path.write_text(header + '{"' + section + '": {' + other + "}}\n")
    args = ["init", "--client", client, "--no-skill", "--no-claude-md"]
    if url:
        args.append("--url")
    assert cli.main(args) == 0
    installed = path.read_text()
    assert other in installed
    assert installed.startswith(header)
    assert '"grag"' in installed
    capsys.readouterr()
    remove = ["init", "--client", client, "--remove"]
    bundles = _bundles()
    assert cli.main([*remove, "--dry-run"]) == 0
    assert "@@" in capsys.readouterr().out
    assert path.read_text() == installed
    assert _bundles() == bundles
    assert cli.main(remove) == 0
    assert other in path.read_text()
    assert '"grag"' not in path.read_text()
    assert path.read_text().startswith(header)
