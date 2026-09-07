from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from grag.cli import _config, main
from grag.project_files import ProjectConfigError
from grag.project_identity import MANIFEST, legacy_database, project_root, read_identity


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GRAG_DB_PATH", raising=False)
    monkeypatch.delenv("GRAG_DB_DIR", raising=False)
    monkeypatch.chdir(tmp_path)


def init(root: Path, monkeypatch, *args: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    assert main([*args, "init", "--no-mcp", "--no-claude-md", "--no-skill"]) == 0


def config(cmd="status", **values):
    return _config(argparse.Namespace(cmd=cmd, db=None, db_dir=None, **values))


def test_same_names_are_isolated_and_mapping_is_idempotent(tmp_path, monkeypatch):
    left, right = tmp_path / "left/api", tmp_path / "right/api"
    init(left, monkeypatch)
    before = (left / MANIFEST).read_bytes()
    a = read_identity(left)
    init(left, monkeypatch)
    assert (left / MANIFEST).read_bytes() == before
    init(right, monkeypatch)
    b = read_identity(right)
    assert a and b and a.checkout_id != b.checkout_id and a.db_path != b.db_path
    assert not Path(a.db_path).exists()  # init does not create an empty DB


@pytest.mark.parametrize(
    "cmd",
    [
        "status",
        "doctor",
        "ingest",
        "ingest-code",
        "export",
        "import",
        "reindex",
        "start",
        "stop",
        "restart",
        "serve",
        "mcp",
    ],
)
def test_all_commands_use_mapping_from_subdirectory(tmp_path, monkeypatch, cmd):
    root = tmp_path / "project"
    init(root, monkeypatch)
    child = root / "src/deep"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert config(cmd, port=None).db_path == Path(read_identity(root).db_path)


def test_saved_custom_port_and_explicit_override(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    assert (
        main(["init", "--port", "43123", "--no-mcp", "--no-claude-md", "--no-skill"])
        == 0
    )
    args = argparse.Namespace(cmd="serve", db=None, db_dir=None, port=None)
    _config(args)
    assert args.port == 43123
    args.port = 54321
    _config(args)
    assert args.port == 54321


def test_selector_precedence_and_env_init(tmp_path, monkeypatch):
    root = tmp_path / "project"
    init(root, monkeypatch)
    env_db = tmp_path / "env.lbdb"
    monkeypatch.setenv("GRAG_DB_PATH", str(env_db))
    assert config().db_path == env_db
    args = argparse.Namespace(cmd="status", db="cli.lbdb", db_dir=None)
    assert _config(args).db_path == Path("cli.lbdb")
    args = argparse.Namespace(cmd="status", db=None, db_dir="dbs")
    assert _config(args).db_dir == Path("dbs")
    assert _config(args).db_path == Path("knowledge.lbdb")
    init(root, monkeypatch)
    assert read_identity(root).db_path == str(env_db)
    monkeypatch.setenv("GRAG_DB_DIR", "dbs")
    assert (
        _config(argparse.Namespace(cmd="status", db="cli.lbdb", db_dir=None)).db_dir
        is None
    )


def test_explicit_sharing_keeps_distinct_checkout_ids(tmp_path, monkeypatch):
    db = tmp_path / "shared.lbdb"
    left, right = tmp_path / "a", tmp_path / "b"
    init(left, monkeypatch, "--db", str(db))
    init(right, monkeypatch, "--db", str(db))
    a, b = read_identity(left), read_identity(right)
    assert a.db_path == b.db_path == str(db)
    assert a.checkout_id != b.checkout_id


def test_copy_requires_fresh_identity_and_move_requires_relocation(
    tmp_path, monkeypatch
):
    old, copy, new = (tmp_path / name for name in ("old", "copy", "new"))
    init(old, monkeypatch)
    original = read_identity(old)
    shutil.copytree(old, copy)
    monkeypatch.chdir(copy)
    with pytest.raises(ProjectConfigError, match="moved or copied"):
        config()
    init(copy, monkeypatch)
    assert read_identity(copy).db_path != original.db_path
    old.rename(new)
    monkeypatch.chdir(new)
    assert main(["init"]) == 1
    assert json.loads((new / MANIFEST).read_text())["root"] == str(old)


def test_real_git_worktree_and_nested_repo_do_not_inherit_mapping(
    tmp_path, monkeypatch
):
    root, worktree = tmp_path / "repo", tmp_path / "worktree"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607 — fixed test argv

    git("init")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    init(root, monkeypatch)
    git("worktree", "add", "--detach", str(worktree))
    init(worktree, monkeypatch)
    assert read_identity(worktree).db_path != read_identity(root).db_path
    ignored = subprocess.run(  # noqa: S603 — fixed test argv
        ["git", "-C", str(root), "check-ignore", str(root / MANIFEST)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 0
    nested = root / "vendor/nested"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    monkeypatch.chdir(nested)
    assert config().db_path == nested / "knowledge.lbdb"


def test_existing_root_database_is_adopted_consistently(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "knowledge.lbdb").touch()
    child = root / "src"
    child.mkdir()
    monkeypatch.chdir(child)
    assert config().db_path == root / "knowledge.lbdb"
    init(child, monkeypatch)
    assert read_identity(root).db_path == str(root / "knowledge.lbdb")


def test_legacy_registration_is_respected_but_basename_alone_is_not(
    tmp_path, monkeypatch
):
    root = tmp_path / "api"
    root.mkdir()
    monkeypatch.chdir(root)
    home_db = Path.home() / ".grag/api.lbdb"
    home_db.parent.mkdir()
    home_db.touch()
    assert main(["init", "--no-mcp", "--no-skill"]) == 1
    assert not (root / MANIFEST).exists()
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "grag": {
                        "command": "grag",
                        "args": ["--db", str(home_db), "mcp", "--port", "43210"],
                    }
                }
            }
        )
    )
    assert config().db_path == home_db
    init(root, monkeypatch)
    assert read_identity(root).db_path == str(home_db)
    assert read_identity(root).port == 43210


@pytest.mark.parametrize(
    "bad", ['{"version": 9}', '{"version": 1, "version": 1}', "[]", "{bad"]
)
def test_invalid_manifest_fails_without_fallback_or_overwrite(
    tmp_path, monkeypatch, bad
):
    root = tmp_path / "repo"
    init(root, monkeypatch)
    (root / MANIFEST).write_text(bad)
    assert main(["status"]) == 1
    assert main(["init"]) == 1
    assert (root / MANIFEST).read_text() == bad


def test_conflicting_legacy_registrations_require_explicit_choice(tmp_path):
    for relative, name in ((".mcp.json", "one"), (".cursor/mcp.json", "two")):
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {"mcpServers": {"grag": {"args": ["--db", str(tmp_path / name)]}}}
            )
        )
    with pytest.raises(ProjectConfigError, match="disagree"):
        legacy_database(tmp_path)


def test_dry_run_creates_no_state_and_invalid_client_prevents_mapping(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    assert main(["init", "--client", "claude", "--dry-run"]) == 0
    assert list(root.iterdir()) == []
    assert not (Path.home() / ".grag").exists()
    (root / ".mcp.json").write_text("bad")
    assert main(["init", "--client", "claude"]) == 1
    assert not (root / MANIFEST).exists()


def test_init_remove_preserves_mapping(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    init(root, monkeypatch)
    before = (root / MANIFEST).read_bytes()
    assert main(["init", "--remove"]) == 0
    assert (root / MANIFEST).read_bytes() == before


def test_subdirectory_init_uses_git_root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    child = tmp_path / "src/deep"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert project_root() == tmp_path
    init(child, monkeypatch)
    assert (tmp_path / MANIFEST).exists()
    assert not (child / MANIFEST).exists()


def test_nested_database_does_not_override_checkout_mapping(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    init(root, monkeypatch)
    child = root / "examples"
    child.mkdir()
    (child / "knowledge.lbdb").touch()
    monkeypatch.chdir(child)
    assert config().db_path == Path(read_identity(root).db_path)


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "http://localhost:8471/mcp/"},
        {"args": ["--db", "relative.lbdb", "mcp"]},
        {"args": ["--db", "/one", "--db", "/two", "mcp"]},
    ],
)
def test_unknown_legacy_selection_cannot_silently_create_new_database(
    tmp_path, monkeypatch, entry
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"grag": entry}}))
    with pytest.raises(ProjectConfigError):
        config()


def test_equals_legacy_flags_are_recognized(tmp_path, monkeypatch):
    db = tmp_path / "chosen.lbdb"
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"grag": {"args": [f"--db={db}", "mcp", "--port=43212"]}}}
        )
    )
    args = argparse.Namespace(cmd="serve", db=None, db_dir=None, port=None)
    assert _config(args).db_path == db
    assert args.port == 43212


def test_remove_does_not_require_valid_database_mapping(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    init(root, monkeypatch)
    (root / MANIFEST).write_text("broken")
    assert main(["init", "--remove"]) == 0
    assert (root / MANIFEST).read_text() == "broken"
