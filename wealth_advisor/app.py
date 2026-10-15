"""Composition root: wires backends -> tools -> registry -> agents -> orchestrator.

This is the only module that knows about concrete implementations. Swapping
the mock CRM for a real one is a change here (or a different `backends`
argument), never in an agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .agents import AdvisorAgent, AnalyzerAgent, DataFetcherAgent
from .config import Settings
from .hitl import AutoApproveReviewer, Reviewer
from .memory import LongTermMemory, SessionMemory, create_long_term_memory
from .orchestrator import Orchestrator
from .tools.advisory import RECOMMEND_TOOL
from .tools.analytics import CASHFLOW_TOOL, PORTFOLIO_TOOL
from .tools.anomaly import CASHFLOW_SCAN_TOOL, COMPLIANCE_SCAN_TOOL, PORTFOLIO_SCAN_TOOL, TXN_SCAN_TOOL
from .tools.base import ToolRegistry
from .tools.client_data import (
    ClientDataSource,
    FileClientDataSource,
    LoadClientDataTool,
    NormalizeClientRecordTool,
)
from .tools.crm import CRMBackend, CRMProfileTool, MockCRMBackend
from .tools.faults import FaultInjector
from .tools.market_data import FxRatesTool, MarketDataBackend, MockMarketDataBackend, QuotesTool
from .tools.narrative import LLMNarrator, NarrativeTool, TemplateNarrator
from .tools.resilience import CircuitBreaker, RetryPolicy
from .tools.scoring import RISK_SCORE_TOOL


@dataclass
class Backends:
    client_data: ClientDataSource
    crm: CRMBackend
    market_data: MarketDataBackend


@dataclass
class Faults:
    crm: str = "none"
    market_data: str = "none"


@dataclass
class System:
    orchestrator: Orchestrator
    registry: ToolRegistry
    session: SessionMemory
    long_term: LongTermMemory
    settings: Settings
    backends: Backends
    faults: Faults = field(default_factory=Faults)


def mock_backends(settings: Settings, faults: Faults) -> Backends:
    data_dir = Path(settings.data_dir)
    crm_db = json.loads((data_dir / "crm.json").read_text())
    market = json.loads((data_dir / "market_data.json").read_text())
    return Backends(
        client_data=FileClientDataSource(data_dir / "clients"),
        crm=MockCRMBackend(crm_db, FaultInjector(faults.crm)),
        market_data=MockMarketDataBackend(market, FaultInjector(faults.market_data)),
    )


def build_registry(backends: Backends, settings: Settings, sleep=None) -> ToolRegistry:
    def retry() -> RetryPolicy:
        kw = dict(max_attempts=settings.tool_max_attempts, base_delay_s=settings.tool_base_delay_s,
                  max_delay_s=settings.tool_max_delay_s)
        if sleep is not None:
            kw["sleep"] = sleep
        return RetryPolicy(**kw)

    def breaker() -> CircuitBreaker:
        return CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_s)

    reg = ToolRegistry()
    # I/O-bound tools: retries + circuit breaker.
    reg.register(LoadClientDataTool(backends.client_data), retry=retry())
    reg.register(CRMProfileTool(backends.crm), retry=retry(), breaker=breaker())
    reg.register(QuotesTool(backends.market_data), retry=retry(), breaker=breaker())
    reg.register(FxRatesTool(backends.market_data), retry=retry(), breaker=breaker())
    if settings.llm_enabled:
        llm = LLMNarrator(settings.llm_model, settings.llm_base_url, timeout_s=settings.llm_timeout_s)
        reg.register(NarrativeTool(llm, name="narrative.llm"),
                     retry=RetryPolicy(max_attempts=2, sleep=sleep or RetryPolicy().sleep))
    # Pure computation: no retries (a deterministic failure will fail again).
    for tool in (NormalizeClientRecordTool(), PORTFOLIO_TOOL, CASHFLOW_TOOL, TXN_SCAN_TOOL, CASHFLOW_SCAN_TOOL,
                 PORTFOLIO_SCAN_TOOL, COMPLIANCE_SCAN_TOOL, RISK_SCORE_TOOL, RECOMMEND_TOOL):
        reg.register(tool)
    reg.register(NarrativeTool(TemplateNarrator(), name="narrative.template"))
    return reg


def build_system(settings: Optional[Settings] = None, *, faults: Optional[Faults] = None,
                 reviewer: Optional[Reviewer] = None, backends: Optional[Backends] = None,
                 session: Optional[SessionMemory] = None, long_term: Optional[LongTermMemory] = None,
                 memory_path: Optional[Path] = None, sleep=None) -> System:
    settings = settings or Settings.from_env()
    faults = faults or Faults()
    backends = backends or mock_backends(settings, faults)
    registry = build_registry(backends, settings, sleep=sleep)
    session = session or SessionMemory()
    long_term = long_term or create_long_term_memory(memory_path)
    agents = [DataFetcherAgent(registry, settings), AnalyzerAgent(registry, settings), AdvisorAgent(registry, settings)]
    orchestrator = Orchestrator(agents, session, long_term, reviewer or AutoApproveReviewer(), settings)
    return System(orchestrator, registry, session, long_term, settings, backends, faults)
