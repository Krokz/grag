"""Bounded local go.mod identity; never invokes Go, downloads, or follows replacements."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from grag.core.limits import read_source


def package_path(file: Path, root: Path, metadata: dict[Path, bytes]) -> str | None:
    """Nearest manifest within the indexed root. Raw bytes also enter freshness checks."""
    for directory in (file.parent, *file.parent.parents):
        if not directory.is_relative_to(root):
            break
        manifest = directory / "go.mod"
        if manifest.is_symlink():
            raise OSError(f"Go module metadata must not be a symlink: {manifest}")
        if not manifest.exists():
            continue
        if manifest not in metadata:
            metadata[manifest] = read_source(manifest, 1024 * 1024)
        # Only the module directive is used. Missing/ambiguous/unsupported syntax
        # prevents path resolution; the file still contributes to freshness.
        text = metadata[manifest].decode("utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        directives = re.findall(
            r'^\s*module\s+("[^"\n]+"|[^\s/][^\s]*)\s*(?://.*)?$', text, re.MULTILINE
        )
        if len(directives) != 1:
            return None
        module = directives[0].strip('"')
        if any(c in module for c in '\\"`') or module.startswith(("/", ".")):
            return None
        relative = file.parent.relative_to(directory).as_posix()
        return module if relative == "." else posixpath.join(module, relative)
    return None
