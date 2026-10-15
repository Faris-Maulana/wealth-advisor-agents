"""Agent contract.

An agent:
  * declares the task types it can handle (`capabilities`) - used by the
    orchestrator for routing;
  * declares which tools it needs (`required_tools`, `optional_tools`) and
    gets a least-privilege `Toolbox` - it cannot reach anything else;
  * never raises to the orchestrator: every outcome is an `AgentResult` with a
    status (ok / degraded / failed), warnings and an optional error.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from ..config import Settings
from ..errors import CriticalStepError
from ..logging_setup import get_logger, log_context
from ..memory import LongTermMemory, SessionMemory
from ..models import StepStatus
from ..tools.base import ToolRegistry


@dataclass
class Task:
    type: str
    payload: dict = field(default_factory=dict)


@dataclass
class RunContext:
    run_id: str
    client_id: str
    session: SessionMemory
    long_term: LongTermMemory
    settings: Settings


@dataclass
class AgentResult:
    status: StepStatus
    output: Any = None
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: float = 0.0


class Agent(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[str]]
    required_tools: ClassVar[frozenset[str]] = frozenset()
    optional_tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, registry: ToolRegistry, settings: Settings):
        available = set(registry.names())
        allowed = set(self.required_tools) | (set(self.optional_tools) & available)
        self.tools = registry.scoped(allowed, owner=self.name)
        self.settings = settings
        self.log = get_logger(f"agents.{self.name}")

    def handle(self, task: Task, ctx: RunContext) -> AgentResult:
        if task.type not in self.capabilities:
            return AgentResult(StepStatus.FAILED, error=f"{self.name} cannot handle task '{task.type}'")
        started = time.perf_counter()
        with log_context(agent=self.name, task=task.type):
            self.log.info("agent.start")
            try:
                result = self.execute(task, ctx)
            except CriticalStepError as exc:
                self.log.error("agent.critical_failure", error=str(exc))
                result = AgentResult(StepStatus.FAILED, error=str(exc))
            except Exception as exc:  # defensive: an agent bug must not crash the orchestrator
                self.log.error("agent.unhandled_exception", error=repr(exc), exc_info=True)
                result = AgentResult(StepStatus.FAILED, error=f"internal error in {self.name}: {exc!r}")
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self.log.info("agent.finish", status=result.status.value, warnings=len(result.warnings),
                          duration_ms=result.duration_ms)
            return result

    def decide(self, decision: str, **rationale: Any) -> None:
        """Log a decision point with its rationale (fallbacks, skips, routing choices)."""
        self.log.info("agent.decision", decision=decision, **rationale)

    @abstractmethod
    def execute(self, task: Task, ctx: RunContext) -> AgentResult:
        ...
