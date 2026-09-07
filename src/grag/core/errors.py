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


class ShutdownError(GragError):
    """New work was refused after shutdown began."""

    def __init__(self):
        super().__init__(
            "The server is shutting down; new work is not accepted.",
            hint="Wait for shutdown to finish, then reconnect to the restarted server.",
        )


class ConflictError(GragError):
    """A mutation precondition or operation-ID payload disagrees with stored state."""

    def __init__(self, message: str, *, code: str, hint: str):
        super().__init__(message, hint=hint)
        self.code = code


class FreshnessError(GragError):
    """A require-fresh read could not verify the code index before its deadline."""

    def __init__(self, message: str, *, freshness: dict, hint: str):
        super().__init__(message, hint=hint)
        self.freshness = freshness
