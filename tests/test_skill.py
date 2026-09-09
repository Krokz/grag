"""Guards for the multi-harness skill packaging.

The grag skill ships to three agent harnesses, each reading a SKILL.md from a
different directory:

    Cursor      -> .cursor/skills/grag/SKILL.md
    Claude Code -> .claude/skills/grag/SKILL.md
    Codex       -> .agents/skills/grag/SKILL.md

The format is identical across all three (YAML frontmatter with ``name`` +
``description``, markdown body), so the copies are byte-for-byte the same. The
sandbox bind-mounts the dot-directories as separate filesystems, so they cannot
be hard/symlinked into one file; these tests fail the suite if the copies drift.

A fourth copy lives at src/grag/assets/skill/SKILL.md — the template shipped in
the wheel that ``grag init`` scaffolds into user projects. It is held to the
same byte-for-byte rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from grag.project import apply_ops, plan_skill_ops, plan_skill_removal_ops
from grag.project_files import ProjectConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]

SKILL_PATHS = [
    REPO_ROOT / ".cursor" / "skills" / "grag" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "grag" / "SKILL.md",
    REPO_ROOT / ".agents" / "skills" / "grag" / "SKILL.md",
]

# The copy shipped inside the wheel — `grag init` scaffolds this one into
# user projects, so it must never drift from the harness copies above.
PACKAGED_TEMPLATE = REPO_ROOT / "src" / "grag" / "assets" / "skill" / "SKILL.md"


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(m.group(1))


def test_skill_copies_exist_and_stay_in_sync():
    contents = []
    for p in [*SKILL_PATHS, PACKAGED_TEMPLATE]:
        assert p.is_file(), f"missing skill copy: {p.relative_to(REPO_ROOT)}"
        contents.append(p.read_bytes())
    assert len(set(contents)) == 1, (
        "skill copies diverged — edit one and re-copy to the rest:\n  "
        + "\n  ".join(
            str(p.relative_to(REPO_ROOT)) for p in [*SKILL_PATHS, PACKAGED_TEMPLATE]
        )
    )
    bundle = {p.relative_to(PACKAGED_TEMPLATE.parent): p.read_bytes()
              for p in PACKAGED_TEMPLATE.parent.rglob("*.md")}
    for path in SKILL_PATHS:
        assert {p.relative_to(path.parent): p.read_bytes() for p in path.parent.rglob("*.md")} == bundle


def test_main_skill_stays_small_and_reference_links_are_packaged():
    text = PACKAGED_TEMPLATE.read_text()
    assert len(text.encode()) < 8_000  # previously 38,589 bytes on every activation
    links = re.findall(r"\]\((references/[^)]+)\)", text)
    assert links
    assert all((PACKAGED_TEMPLATE.parent / target).is_file() for target in links)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_init_installs_and_upgrades_the_whole_bundle(tmp_path, monkeypatch, newline):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / ".claude/skills/grag"
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    for source in PACKAGED_TEMPLATE.parent.rglob("*.md"):
        target = root / source.relative_to(PACKAGED_TEMPLATE.parent)
        # New files use canonical LF even when Git checked out CRLF templates.
        assert target.read_bytes() == source.read_text(encoding="utf-8").encode("utf-8")
    reference = root / "references/memory.md"
    reference.write_bytes(("<!-- grag-managed skill reference: memory -->\nold\n").replace("\n", newline).encode())
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    assert reference.read_bytes() == (PACKAGED_TEMPLATE.parent / "references/memory.md").read_text(encoding="utf-8").replace("\n", newline).encode("utf-8")
    assert plan_skill_ops(["claude"], tmp_path) == []
    apply_ops(plan_skill_removal_ops(["claude"], tmp_path))
    assert list(root.rglob("*.md")) == []


def test_foreign_reference_collision_does_not_publish_a_partial_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / ".claude/skills/grag"
    reference = root / "references/memory.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("My own memory procedures\n")
    with pytest.raises(ProjectConfigError, match="user content"):
        plan_skill_ops(["claude"], tmp_path)
    assert reference.read_text() == "My own memory procedures\n"
    assert list(root.rglob("*.md")) == [reference]


def test_removal_preserves_modified_reference_and_custom_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / ".claude/skills/grag"
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    reference = root / "references/memory.md"
    reference.write_text(reference.read_text() + "Local advice\n")
    apply_ops(plan_skill_removal_ops(["claude"], tmp_path))
    assert reference.read_text().endswith("Local advice\n")
    assert not (root / "SKILL.md").exists()
    apply_ops(plan_skill_ops(["claude"], tmp_path))
    (root / "SKILL.md").write_text("Local entrypoint using references\n")
    apply_ops(plan_skill_removal_ops(["claude"], tmp_path))
    assert len(list(root.rglob("*.md"))) == 4


def test_skill_frontmatter_valid_for_all_harnesses():
    text = SKILL_PATHS[0].read_text()
    fm = _frontmatter(text)
    # name: kebab-case, matches the directory, required by all three harnesses.
    assert re.fullmatch(r"[a-z0-9-]+", fm["name"]), "name must be kebab-case"
    assert fm["name"] == "grag"
    for p in SKILL_PATHS:
        assert p.parent.name == fm["name"], (
            f"{p}: directory name must match frontmatter name (Claude Code requirement)"
        )
    # description present and non-trivial (drives skill discovery/triggering).
    assert len(fm["description"]) > 20
