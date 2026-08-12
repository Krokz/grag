"""Validation for Cypher identifiers interpolated into generated statements."""

from __future__ import annotations

import re

from grag.core.errors import SchemaError

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str, what: str = "identifier") -> str:
    """Return *name* when it is safe to interpolate as a Cypher identifier."""
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise SchemaError(
            f"Invalid {what} {name!r}.",
            hint="Identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$ "
            "(start with a letter or underscore; then letters, digits, underscores).",
        )
    return name
