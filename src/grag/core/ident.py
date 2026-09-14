"""Validation for Cypher identifiers interpolated into generated statements."""

from __future__ import annotations

import re

from grag.core.errors import SchemaError

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Ladybug v0.20.3: src/antlr4/keywords.txt minus iC_NonReservedKeywords
# in src/antlr4/Cypher.g4. Keep this aligned with the pinned engine, not a
# generic SQL/Cypher list: e.g. MATCH, RETURN, TYPE and LIMIT are allowed here.
# https://github.com/LadybugDB/ladybug/blob/v0.20.3/src/antlr4/Cypher.g4
RESERVED_KEYWORDS = frozenset({
    "ACYCLIC", "ALL", "AND", "ANY", "ASC", "ASCENDING",
    "CASE", "CAST", "COLUMN", "COMMIT_SKIP_CHECKPOINT", "CREATE", "CSR",
    "DBTYPE", "DEFAULT", "DESC", "DESCENDING", "DISTINCT", "ELSE",
    "END", "ENDS", "EXISTS", "FALSE", "FOR", "GLOB",
    "GROUP", "HEADERS", "HINT", "IN", "INDEX", "INSTALL",
    "JOIN", "MACRO", "MULTI_JOIN", "NONE", "NOT", "NULL",
    "ON", "ONLY", "OPTIONAL", "OPTIONS", "OR", "ORDER",
    "PRIMARY", "PROFILE", "ROLLBACK_SKIP_CHECKPOINT", "SHORTEST", "SINGLE", "SORTED",
    "STARTS", "TABLE", "THEN", "TRAIL", "TRUE", "UNION",
    "UNWIND", "WHEN", "WHERE", "WITH", "WSHORTEST", "XOR",
})


def validate_identifier(name: str, what: str = "identifier") -> str:
    """Return *name* when safe to interpolate as an unquoted Cypher identifier."""
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise SchemaError(
            f"Invalid {what} {name!r}.",
            hint="Identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$ "
            "(start with a letter or underscore; then letters, digits, underscores).",
        )
    if name.upper() in RESERVED_KEYWORDS:
        raise SchemaError(
            f"Reserved {what} {name!r}: {name.upper()} is a Cypher keyword.",
            hint=f"Rename it, for example to '{name}_value'. Grag schema names use "
            "unquoted identifiers; changing case or adding backticks does not bypass this rule.",
        )
    return name
