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
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

SKILL_PATHS = [
    REPO_ROOT / ".cursor" / "skills" / "grag" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "grag" / "SKILL.md",
    REPO_ROOT / ".agents" / "skills" / "grag" / "SKILL.md",
]


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(m.group(1))


def test_skill_copies_exist_and_stay_in_sync():
    contents = []
    for p in SKILL_PATHS:
        assert p.is_file(), f"missing skill copy: {p.relative_to(REPO_ROOT)}"
        contents.append(p.read_bytes())
    assert len(set(contents)) == 1, (
        "skill copies diverged — edit one and re-copy to the other two:\n  "
        + "\n  ".join(str(p.relative_to(REPO_ROOT)) for p in SKILL_PATHS)
    )


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
