"""Exception hierarchy.

Tool errors carry a `retryable` flag so the resilience layer can decide between
retrying (transient: timeouts, 5xx) and failing fast (permanent: 404, bad input,
schema violations). Agents never see raw backend exceptions; the registry
converts everything into `ToolResult` objects.
"""

from __future__ import annotations


class WealthAdvisorError(Exception):
    """Base class for all domain errors."""


# --- tool layer -------------------------------------------------------------

class ToolError(WealthAdvisorError):
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class TransientToolError(ToolError):
    """Upstream temporarily unavailable (HTTP 5xx, connection reset)."""
    retryable = True


class ToolTimeoutError(TransientToolError):
    """Call exceeded its latency budget."""


class PermanentToolError(ToolError):
    """Retrying will not help (bad request, schema violation)."""
    retryable = False


class ResourceNotFoundError(PermanentToolError):
    """Requested entity does not exist upstream (HTTP 404)."""


class ToolInputError(PermanentToolError):
    """Arguments failed the tool's declared input schema."""


class SchemaValidationError(PermanentToolError):
    """Upstream returned a payload that violates the expected contract."""


class CircuitOpenError(ToolError):
    """Circuit breaker is open; call short-circuited without hitting upstream."""
    retryable = False


class ToolNotFoundError(ToolError):
    """Tool is not registered or not permitted for the calling agent."""


class ToolPermissionError(ToolNotFoundError):
    """Agent attempted to call a tool outside its allow-list."""


# --- data / orchestration ---------------------------------------------------

class DataValidationError(WealthAdvisorError):
    """Client input data is unusable (as opposed to partially dirty)."""


class CriticalStepError(WealthAdvisorError):
    """A step the pipeline cannot proceed without has failed."""


class RunNotFoundError(WealthAdvisorError):
    """No checkpoint exists for the requested run id."""


class InvalidRunStateError(WealthAdvisorError):
    """Operation not allowed in the run's current state (e.g. resume a finished run)."""
