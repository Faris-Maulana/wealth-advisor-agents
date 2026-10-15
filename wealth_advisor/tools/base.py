"""Tool abstraction layer.

Agents never import backends (CRM client, market-data client, detectors)
directly. They receive a `Toolbox`: an allow-listed view over the
`ToolRegistry`. The registry owns the cross-cutting concerns:

* input validation against the tool's declared `ToolSpec`
* retries with exponential backoff for retryable errors
* a circuit breaker per tool
* structured logging of every call (latency, attempts, outcome)
* converting every failure into a `ToolResult` instead of an exception

Swapping a mock for a real implementation is a registration change, not an
agent change.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..errors import (
    CircuitOpenError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolPermissionError,
)
from ..logging_setup import get_logger
from .resilience import NO_RETRY, CircuitBreaker, RetryPolicy

log = get_logger("tools")


@dataclass(frozen=True)
class ParamSpec:
    type: type | tuple[type, ...]
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, ParamSpec] = field(default_factory=dict)
    idempotent: bool = True           # only idempotent tools are retried

    def validate(self, kwargs: dict) -> None:
        unknown = set(kwargs) - set(self.params)
        if unknown:
            raise ToolInputError(f"{self.name}: unexpected argument(s) {sorted(unknown)}")
        for pname, spec in self.params.items():
            if pname not in kwargs or kwargs[pname] is None:
                if spec.required:
                    raise ToolInputError(f"{self.name}: missing required argument '{pname}'")
                continue
            if not isinstance(kwargs[pname], spec.type):
                raise ToolInputError(
                    f"{self.name}: argument '{pname}' expected {spec.type}, got {type(kwargs[pname]).__name__}")


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    attempts: int = 1
    latency_ms: float = 0.0

    def unwrap(self) -> Any:
        if not self.ok:
            raise ToolError(f"{self.tool} failed: {self.error_type}: {self.error}")
        return self.data


class Tool(ABC):
    """A capability with a declared contract. Implementations raise `ToolError`s."""

    spec: ToolSpec

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        ...

    @property
    def name(self) -> str:
        return self.spec.name


class FunctionTool(Tool):
    """Adapter so pure functions (detectors, calculators) can be tools too."""

    def __init__(self, spec: ToolSpec, fn: Callable[..., Any]):
        self.spec = spec
        self._fn = fn

    def run(self, **kwargs: Any) -> Any:
        return self._fn(**kwargs)


@dataclass
class _Registration:
    tool: Tool
    retry: RetryPolicy
    breaker: Optional[CircuitBreaker]


class ToolRegistry:
    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._tools: dict[str, _Registration] = {}
        self._clock = clock

    def register(self, tool: Tool, *, retry: Optional[RetryPolicy] = None,
                 breaker: Optional[CircuitBreaker] = None) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' already registered")
        policy = retry if (retry and tool.spec.idempotent) else NO_RETRY
        self._tools[tool.name] = _Registration(tool, policy, breaker)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name].tool.spec

    def breaker_state(self, name: str) -> Optional[str]:
        reg = self._tools.get(name)
        return reg.breaker.state if reg and reg.breaker else None

    def reset_breakers(self) -> None:
        for reg in self._tools.values():
            if reg.breaker:
                reg.breaker.reset()

    def scoped(self, allowed: Iterable[str], owner: str) -> "Toolbox":
        allowed = frozenset(allowed)
        missing = allowed - set(self._tools)
        if missing:
            raise ToolNotFoundError(f"agent '{owner}' requires unregistered tools: {sorted(missing)}")
        return Toolbox(self, allowed, owner)

    def invoke(self, name: str, **kwargs: Any) -> ToolResult:
        reg = self._tools.get(name)
        if reg is None:
            return ToolResult(name, ok=False, error=f"unknown tool '{name}'", error_type="ToolNotFoundError",
                              attempts=0)
        started = self._clock()
        attempts = 0
        try:
            reg.tool.spec.validate(kwargs)
        except ToolInputError as exc:
            log.error("tool.invalid_input", tool=name, error=str(exc))
            return ToolResult(name, ok=False, error=str(exc), error_type=type(exc).__name__, attempts=0)

        delays = iter(reg.retry.backoff_delays())
        last_exc: Optional[BaseException] = None
        while True:
            if reg.breaker and not reg.breaker.allow():
                last_exc = CircuitOpenError(f"circuit open for '{name}'")
                log.warning("tool.circuit_open", tool=name)
                break
            attempts += 1
            try:
                data = reg.tool.run(**kwargs)
            except ToolError as exc:
                last_exc = exc
                if reg.breaker and exc.retryable:
                    reg.breaker.record_failure()
                delay = next(delays, None) if exc.retryable else None
                log.warning("tool.attempt_failed", tool=name, attempt=attempts, error_type=type(exc).__name__,
                            error=str(exc), will_retry=delay is not None)
                if delay is None:
                    break
                reg.retry.sleep(delay)
                continue
            except Exception as exc:  # bug in tool implementation: do not retry, do not crash the run
                last_exc = exc
                log.error("tool.unhandled_exception", tool=name, attempt=attempts, error=repr(exc), exc_info=True)
                break
            else:
                if reg.breaker:
                    reg.breaker.record_success()
                latency = (self._clock() - started) * 1000
                log.info("tool.success", tool=name, attempts=attempts, latency_ms=round(latency, 2))
                return ToolResult(name, ok=True, data=data, attempts=attempts, latency_ms=latency)

        latency = (self._clock() - started) * 1000
        log.error("tool.failed", tool=name, attempts=attempts, error_type=type(last_exc).__name__,
                  error=str(last_exc), latency_ms=round(latency, 2))
        return ToolResult(name, ok=False, error=str(last_exc), error_type=type(last_exc).__name__,
                          attempts=attempts, latency_ms=latency)


class Toolbox:
    """Least-privilege view of the registry handed to a single agent."""

    def __init__(self, registry: ToolRegistry, allowed: frozenset[str], owner: str):
        self._registry = registry
        self._allowed = allowed
        self.owner = owner

    @property
    def names(self) -> list[str]:
        return sorted(self._allowed)

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        if name not in self._allowed:
            exc = ToolPermissionError(f"agent '{self.owner}' is not permitted to use tool '{name}'")
            log.error("tool.permission_denied", tool=name, agent=self.owner)
            return ToolResult(name, ok=False, error=str(exc), error_type=type(exc).__name__, attempts=0)
        return self._registry.invoke(name, **kwargs)
