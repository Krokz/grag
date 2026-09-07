"""Error types. Every grag error carries an optional `hint`: a concrete
correction path, designed to be read by an LLM in a tool-call loop."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError


class GragError(Exception):
    code = "grag_error"

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\nHINT: {self.hint}"
        return self.message

    def to_dict(self) -> dict:
        return {"error": self.message, "hint": self.hint, "code": self.code}


class CypherError(GragError):
    """Query parse/execution failure from the engine."""
    code = "cypher_error"


class ResourceLimitError(GragError):
    """Admission or application work exceeded a documented finite bound."""
    code = "resource_limit"

    def __init__(self, resource: str, limit: int, *, hint: str | None = None):
        super().__init__(f"Resource limit reached: {resource} (maximum {limit}).",
                         hint=hint or "Narrow the labels, node IDs or query projection, reduce the batch, or page the requested text.")
        self.resource, self.limit = resource, limit

    def to_dict(self) -> dict:
        return {**super().to_dict(), "resource": self.resource, "limit": self.limit}


class SchemaError(GragError):
    """Unknown label/property, type mismatch, or invalid schema definition."""
    code = "schema_error"


class NotFoundError(GragError):
    """Referenced node/edge/table does not exist."""
    code = "not_found"


class ReadOnlyViolation(GragError):
    """A write keyword was used on the read-only query path."""
    code = "read_only_violation"


class ConfigurationError(GragError):
    """Missing or invalid configuration (e.g. embedder not installed)."""
    code = "configuration_error"


class ShutdownError(GragError):
    """New work was refused after shutdown began."""
    code = "shutting_down"

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
    code = "freshness_unavailable"

    def __init__(self, message: str, *, freshness: dict, hint: str):
        super().__init__(message, hint=hint)
        self.freshness = freshness

    def to_dict(self) -> dict:
        return {**super().to_dict(), "freshness": self.freshness}


def validation_error_body(exc: ValidationError | Sequence[dict]) -> dict:
    """Same bounded, value-free validation details in REST and MCP errors."""
    errors = exc.errors(include_input=False, include_url=False) if isinstance(exc, ValidationError) else exc
    details = [{"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]} for e in errors[:5]]
    summary = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['message']}" for e in details)
    omitted = max(0, len(errors) - len(details))
    if omitted:
        summary += f"; … ({omitted} more)"
    return {
        "code": "validation_error", "error": f"Invalid arguments — {summary}",
        "hint": "Check the tool input schema for the expected shape and field names.",
        "details": details, "omitted_errors": omitted,
    }
