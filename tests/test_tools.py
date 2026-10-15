"""Tool layer: contract validation, retries, circuit breaker, least privilege."""

from __future__ import annotations

import pytest

from wealth_advisor.errors import PermanentToolError, TransientToolError
from wealth_advisor.tools.base import ParamSpec, Tool, ToolRegistry, ToolSpec
from wealth_advisor.tools.crm import CRMProfileTool, MockCRMBackend
from wealth_advisor.tools.faults import FaultInjector
from wealth_advisor.tools.resilience import CircuitBreaker, RetryPolicy


class FlakyTool(Tool):
    spec = ToolSpec("flaky", "fails N times", {"x": ParamSpec(int)})

    def __init__(self, fail_times: int, exc=TransientToolError):
        self.fail_times, self.exc, self.calls = fail_times, exc, 0

    def run(self, x: int) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc("boom")
        return x * 2


def registry_with(tool, attempts=3, breaker=None):
    reg = ToolRegistry()
    sleeps: list[float] = []
    reg.register(tool, retry=RetryPolicy(max_attempts=attempts, sleep=sleeps.append), breaker=breaker)
    return reg, sleeps


def test_transient_failure_is_retried_then_succeeds():
    tool = FlakyTool(fail_times=2)
    reg, sleeps = registry_with(tool)
    res = reg.invoke("flaky", x=21)
    assert res.ok and res.data == 42 and res.attempts == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff


def test_permanent_failure_is_not_retried():
    tool = FlakyTool(fail_times=5, exc=PermanentToolError)
    reg, sleeps = registry_with(tool)
    res = reg.invoke("flaky", x=1)
    assert not res.ok and res.attempts == 1 and sleeps == []
    assert res.error_type == "PermanentToolError"


def test_retries_exhausted_returns_error_result_not_exception():
    reg, _ = registry_with(FlakyTool(fail_times=10))
    res = reg.invoke("flaky", x=1)
    assert not res.ok and res.attempts == 3 and res.error_type == "TransientToolError"


def test_input_validation_rejects_bad_arguments():
    reg, _ = registry_with(FlakyTool(0))
    assert reg.invoke("flaky", x="nope").error_type == "ToolInputError"
    assert reg.invoke("flaky").error_type == "ToolInputError"
    assert reg.invoke("flaky", x=1, y=2).error_type == "ToolInputError"


def test_unknown_tool():
    assert ToolRegistry().invoke("missing").error_type == "ToolNotFoundError"


def test_unhandled_exception_in_tool_is_contained():
    class Buggy(Tool):
        spec = ToolSpec("buggy", "raises KeyError")

        def run(self):
            raise KeyError("bug")

    reg = ToolRegistry()
    reg.register(Buggy())
    res = reg.invoke("buggy")
    assert not res.ok and res.error_type == "KeyError"


def test_circuit_breaker_opens_and_short_circuits():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=10, clock=lambda: now[0])
    tool = FlakyTool(fail_times=100)
    reg, _ = registry_with(tool, attempts=3, breaker=breaker)
    assert reg.invoke("flaky", x=1).attempts == 3
    assert breaker.state == "open"
    calls_before = tool.calls
    res = reg.invoke("flaky", x=1)
    assert res.error_type == "CircuitOpenError" and tool.calls == calls_before  # upstream not hit
    now[0] = 11
    assert breaker.state == "half_open"
    tool.fail_times = 0
    assert reg.invoke("flaky", x=1).ok and breaker.state == "closed"


def test_toolbox_enforces_least_privilege():
    reg, _ = registry_with(FlakyTool(0))
    box = reg.scoped(set(), owner="analyzer")
    res = box.call("flaky", x=1)
    assert not res.ok and res.error_type == "ToolPermissionError"


def test_crm_tool_maps_404_and_corrupt_payload():
    db = {"C1": {"client_id": "C1", "risk_profile": "moderate", "kyc_status": "verified", "kyc_expiry": "2027-01-01"}}
    reg = ToolRegistry()
    reg.register(CRMProfileTool(MockCRMBackend(db)), retry=RetryPolicy(sleep=lambda s: None))
    assert reg.invoke("crm.get_client_profile", client_id="C1").data.risk_profile == "moderate"
    nf = reg.invoke("crm.get_client_profile", client_id="NOPE")
    assert nf.error_type == "ResourceNotFoundError" and nf.attempts == 1

    reg2 = ToolRegistry()
    reg2.register(CRMProfileTool(MockCRMBackend(db, FaultInjector("corrupt"))), retry=RetryPolicy(sleep=lambda s: None))
    bad = reg2.invoke("crm.get_client_profile", client_id="C1")
    assert bad.error_type == "SchemaValidationError" and bad.attempts == 1


@pytest.mark.parametrize("mode,expected", [("transient", True), ("down", False), ("slow", False)])
def test_fault_modes(mode, expected):
    db = {"C1": {"client_id": "C1", "risk_profile": "moderate", "kyc_status": "verified", "kyc_expiry": "2027-01-01"}}
    reg = ToolRegistry()
    reg.register(CRMProfileTool(MockCRMBackend(db, FaultInjector(mode))), retry=RetryPolicy(sleep=lambda s: None))
    assert reg.invoke("crm.get_client_profile", client_id="C1").ok is expected
