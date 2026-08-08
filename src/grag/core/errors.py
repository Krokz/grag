"""Error types. Every grag error carries an optional `hint`: a concrete
correction path, designed to be read by an LLM in a tool-call loop."""

from __future__ import annotations


class GragError(Exception):
    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\nHINT: {self.hint}"
        return self.message

    def to_dict(self) -> dict:
        return {"error": self.message, "hint": self.hint}


class CypherError(GragError):
    """Query parse/execution failure from the engine."""


class SchemaError(GragError):
    """Unknown label/property, type mismatch, or invalid schema definition."""


class NotFoundError(GragError):
    """Referenced node/edge/table does not exist."""


class ReadOnlyViolation(GragError):
    """A write keyword was used on the read-only query path."""


class ConfigurationError(GragError):
    """Missing or invalid configuration (e.g. embedder not installed)."""
