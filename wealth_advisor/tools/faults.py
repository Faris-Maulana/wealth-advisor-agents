"""Fault injection for mock backends.

Lets the demo and the test suite exercise every failure path of the
resilience layer deterministically:

  none       healthy
  transient  first N calls fail with a retryable 503, then recover
  down       every call fails with a retryable connection error
  slow       every call exceeds the latency budget (timeout)
  corrupt    calls succeed but return a payload that violates the contract
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ToolTimeoutError, TransientToolError

FAULT_MODES = ("none", "transient", "down", "slow", "corrupt")


@dataclass
class FaultInjector:
    mode: str = "none"
    transient_failures: int = 2
    simulated_latency_ms: float = 2500.0
    timeout_ms: float = 1000.0
    calls: int = 0

    def __post_init__(self) -> None:
        if self.mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode '{self.mode}', expected one of {FAULT_MODES}")

    def before_call(self, service: str) -> None:
        self.calls += 1
        if self.mode == "transient" and self.calls <= self.transient_failures:
            raise TransientToolError(f"{service}: HTTP 503 Service Unavailable (simulated, call {self.calls})")
        if self.mode == "down":
            raise TransientToolError(f"{service}: connection refused (simulated outage)")
        if self.mode == "slow" and self.simulated_latency_ms > self.timeout_ms:
            raise ToolTimeoutError(
                f"{service}: no response within {self.timeout_ms:.0f}ms (simulated latency {self.simulated_latency_ms:.0f}ms)")

    @property
    def corrupt(self) -> bool:
        return self.mode == "corrupt"
