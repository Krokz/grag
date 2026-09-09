"""One bounded source-selection policy for indexing and freshness checks."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from pathspec import GitIgnoreSpec

from grag.core.errors import GragError
from grag.core.limits import charge, read_source

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        ".idea",
        ".vscode",
    }
)


def enclosing_root(path: Path) -> Path:
    directory = path if path.is_dir() else path.parent
    return next(
        (
            p
            for p in (directory, *directory.parents)
            if (p / ".git").exists() or (p / ".grag/project.json").exists()
        ),
        directory,
    )


def path_roots(
    paths: list[Path], root: str | None = None, *, registered: list[Path] | None = None
) -> dict[Path, Path]:
    """Directories remain explicit roots; files use their enclosing checkout."""
    explicit = Path(root).expanduser().resolve() if root else None
    normalized = []
    for item in paths:
        path = Path(os.path.abspath(item.expanduser()))
        boundary = explicit or enclosing_root(path).resolve()
        # Canonicalize aliases above the scope (e.g. macOS /var -> /private/var),
        # retaining lexical components inside it so symlinks remain detectable.
        anchor = next(
            (p for p in (path, *path.parents) if p.resolve() == boundary), None
        )
        if anchor is None:
            raise GragError(f"Source {path} is outside root {boundary}")
        normalized.append(boundary / path.relative_to(anchor))
    paths = normalized
    directories = sorted((p for p in paths if p.is_dir()), key=lambda p: len(p.parts))
    result = {}
    for path in paths:
        selected = (
            explicit
            or (path if path.is_dir() else None)
            or next((p for p in directories if path.is_relative_to(p)), None)
        )
        boundary = enclosing_root(path)
        selected = selected or next(
            (
                p
                for p in sorted(
                    registered or [], key=lambda p: len(p.parts), reverse=True
                )
                if path.is_relative_to(p) and p.is_relative_to(boundary)
            ),
            None,
        )
        selected = selected or boundary
        if not path.is_relative_to(selected):
            raise GragError(f"Source {path} is outside root {selected}")
        result[path] = selected
    return result


class Selection:
    def __init__(self, root: Path):
        self.root = root
        self.policy_root = enclosing_root(root)
        self._specs: dict[Path, list[GitIgnoreSpec]] = {}

    def _rules(self, directory: Path) -> list[GitIgnoreSpec]:
        if directory not in self._specs:
            specs = []
            for name in (".gitignore", ".gragignore"):
                path = directory / name
                if path.is_symlink():
                    raise GragError(f"Ignore file must not be a symlink: {path}")
                if path.exists():
                    try:
                        specs.append(
                            GitIgnoreSpec.from_lines(
                                read_source(path, 1024 * 1024)
                                .decode("utf-8")
                                .splitlines()
                            )
                        )
                    except (OSError, UnicodeError, ValueError) as exc:
                        raise GragError(
                            f"Cannot read ignore policy {path}: {exc}"
                        ) from exc
            self._specs[directory] = specs
        return self._specs[directory]

    def reason(self, path: Path, *, directory: bool = False) -> str | None:
        relative = path.relative_to(self.policy_root)
        current = self.policy_root
        for i, part in enumerate(relative.parts):
            child = current / part
            is_dir = i < len(relative.parts) - 1 or directory
            if child.is_symlink():
                return "symlink boundary"
            if is_dir and part in SKIP_DIRS:
                return "generated/VCS directory"
            if (
                is_dir
                and child != self.root
                and child.is_relative_to(self.root)
                and (child / ".git").exists()
            ):
                return "nested repository/worktree (index it explicitly)"
            # Evaluate every ancestor's rules, in increasing specificity.
            ignored = False
            for parent in reversed((current, *current.parents)):
                if not parent.is_relative_to(self.policy_root):
                    continue
                name = child.relative_to(parent).as_posix() + ("/" if is_dir else "")
                for spec in self._rules(parent):
                    match = spec.check_file(name)
                    if match.include is not None:
                        ignored = match.include
            if ignored:
                return "ignore policy"
            current = child
        return None


def selected_files(
    paths: list[Path],
    warnings: list[str],
    *,
    root: str | None = None,
    errors: list[str] | None = None,
) -> Iterator[tuple[Path, Path]]:
    seen: set[Path] = set()
    policies: dict[Path, Selection] = {}

    def failed(message: str) -> None:
        warnings.append(message)
        if errors is not None:
            errors.append(message)

    for path, selected in path_roots(paths, root).items():
        policy = policies.setdefault(selected, Selection(selected))
        reason = policy.reason(path, directory=path.is_dir())
        if reason:
            warnings.append(f"skipped {path}: {reason}")
            continue
        if path.is_file():
            charge("source_entries")
            if path not in seen:
                seen.add(path)
                yield selected, path
        elif path.is_dir():
            for directory, dirs, names in os.walk(
                path, onerror=lambda exc: failed(f"Cannot scan {exc.filename}: {exc}")
            ):
                charge("source_entries", 1 + len(dirs) + len(names))
                base = Path(directory)
                kept = []
                for name in sorted(dirs):
                    reason = policy.reason(base / name, directory=True)
                    if reason:
                        warnings.append(f"skipped {base / name}: {reason}")
                    else:
                        kept.append(name)
                dirs[:] = kept
                for name in sorted(names):
                    file = base / name
                    if file not in seen and not policy.reason(file):
                        seen.add(file)
                        yield selected, file
        else:
            failed(f"skipped {path}: file not found (no such file or directory)")
