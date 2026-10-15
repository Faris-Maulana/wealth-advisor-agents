"""Typed domain models shared across agents.

Everything is a plain dataclass so that run state can be checkpointed as JSON
(needed for asynchronous human review) and reconstructed with `from_dict`.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import sys
import typing
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


# --- serialisation helpers ---------------------------------------------------

def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def _coerce(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union or (sys.version_info >= (3, 10) and origin is getattr(__import__("types"), "UnionType", None)):
        non_none = [a for a in args if a is not type(None)]
        return _coerce(non_none[0], value) if len(non_none) == 1 else value
    if origin in (list, tuple, set, frozenset):
        inner = args[0] if args else Any
        items = [_coerce(inner, v) for v in value]
        return origin(items) if origin is not list else items
    if origin is dict:
        vt = args[1] if len(args) == 2 else Any
        return {k: _coerce(vt, v) for k, v in value.items()}
    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            return from_dict(tp, value)
        if issubclass(tp, enum.Enum):
            return tp(value)
        if tp is datetime and isinstance(value, str):
            return datetime.fromisoformat(value)
        if tp is date and isinstance(value, str):
            return date.fromisoformat(value)
    return value


T = typing.TypeVar("T")


def from_dict(cls: type[T], data: dict) -> T:
    """Reconstruct a (possibly nested) dataclass from `to_jsonable` output."""
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _coerce(hints[f.name], data[f.name])
    return cls(**kwargs)


# --- enums -------------------------------------------------------------------

class Severity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["low", "medium", "high", "critical"].index(self.value)

    def __ge__(self, other: "Severity") -> bool:  # type: ignore[override]
        return self.rank >= other.rank

    def __gt__(self, other: "Severity") -> bool:  # type: ignore[override]
        return self.rank > other.rank


class FindingStatus(str, enum.Enum):
    NEW = "new"
    RECURRING = "recurring"
    SUPPRESSED = "suppressed"        # previously dismissed by a reviewer
    CONFIRMED = "confirmed"          # confirmed in this run's review
    DISMISSED = "dismissed"          # dismissed in this run's review


class StepStatus(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    REJECTED = "rejected"
    FAILED = "failed"


# --- input data --------------------------------------------------------------

@dataclass
class Account:
    account_id: str
    type: str
    balance: float
    currency: str = "USD"


@dataclass
class Holding:
    ticker: str
    quantity: float
    cost_basis: float


@dataclass
class Transaction:
    txn_id: str
    timestamp: datetime
    amount: float                     # base currency; negative = outflow
    category: str
    counterparty: str
    channel: str = "card"
    country: str = "US"
    account_id: str = ""
    original_amount: Optional[float] = None
    original_currency: str = "USD"

    @property
    def is_outflow(self) -> bool:
        return self.amount < 0


@dataclass
class DataQualityIssue:
    source: str
    code: str
    message: str
    record_id: Optional[str] = None
    severity: str = "warning"         # warning | error


@dataclass
class ClientProfile:
    """CRM view of the client."""
    client_id: str
    risk_profile: Optional[str] = None
    risk_profile_updated: Optional[date] = None
    kyc_status: Optional[str] = None
    kyc_expiry: Optional[date] = None
    age: Optional[int] = None
    advisor: Optional[str] = None
    segment: Optional[str] = None
    investment_horizon_years: Optional[int] = None
    usual_countries: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    last_contact: Optional[date] = None
    source: str = "crm"               # crm | cache | unavailable
    stale: bool = False
    fetched_at: Optional[str] = None


@dataclass
class Quote:
    ticker: str
    price: float
    asset_class: str
    sector: str
    volatility: float


@dataclass
class ClientContext:
    """Everything the Data Fetcher assembles for downstream analysis."""
    client_id: str
    name: str
    as_of: date
    base_currency: str
    home_country: str
    accounts: list[Account]
    holdings: list[Holding]
    transactions: list[Transaction]
    profile: ClientProfile
    quotes: dict[str, Quote] = field(default_factory=dict)
    data_quality: list[DataQualityIssue] = field(default_factory=list)
    degraded_sources: list[str] = field(default_factory=list)


# --- analysis outputs --------------------------------------------------------

@dataclass
class Anomaly:
    type: str
    category: str                     # transaction | cashflow | portfolio | compliance
    severity: Severity
    score: float                      # 0..1 confidence-weighted strength
    title: str
    description: str
    evidence: dict = field(default_factory=dict)
    transaction_ids: list[str] = field(default_factory=list)
    fingerprint: str = ""
    status: FindingStatus = FindingStatus.NEW
    reviewer_note: Optional[str] = None
    original_severity: Optional[Severity] = None

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = make_fingerprint(self.type, self.evidence.get("key") or self.transaction_ids)

    @property
    def counts_toward_risk(self) -> bool:
        return self.status not in (FindingStatus.SUPPRESSED, FindingStatus.DISMISSED)


def make_fingerprint(kind: str, key: Any) -> str:
    raw = json.dumps([kind, key], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class PortfolioSummary:
    total_market_value: float
    invested_value: float
    cash_value: float
    allocation: dict[str, float]
    target_allocation: dict[str, float]
    sector_weights: dict[str, float]
    position_weights: dict[str, float]
    position_sectors: dict[str, str]
    unrealized_pnl: float
    weighted_volatility: Optional[float]
    priced_with_fallback: list[str] = field(default_factory=list)
    excluded_holdings: list[str] = field(default_factory=list)


@dataclass
class CashflowSummary:
    months: list[str]
    monthly_income: dict[str, float]
    monthly_spend: dict[str, float]
    avg_monthly_income: float
    avg_monthly_spend: float
    savings_rate: Optional[float]
    top_spend_categories: dict[str, float]
    liquid_assets: float
    cash_buffer_months: Optional[float]
    transaction_count: int


@dataclass
class RiskScore:
    value: float
    band: str
    drivers: list[str]
    previous_value: Optional[float] = None
    previous_band: Optional[str] = None
    delta: Optional[float] = None


@dataclass
class AnalysisResult:
    portfolio: Optional[PortfolioSummary]
    cashflow: Optional[CashflowSummary]
    anomalies: list[Anomaly]
    risk: RiskScore
    notes: list[str] = field(default_factory=list)


@dataclass
class Recommendation:
    id: str
    priority: str                     # P1 | P2 | P3
    category: str
    action: str
    rationale: str
    linked_findings: list[str] = field(default_factory=list)
    requires_approval: bool = False


# --- review (HITL) -----------------------------------------------------------

@dataclass
class FindingDecision:
    fingerprint: str
    action: str                       # confirm | dismiss | override
    severity: Optional[Severity] = None
    note: Optional[str] = None


@dataclass
class ReviewDecision:
    approved: bool
    reviewer: str
    notes: str = ""
    finding_decisions: list[FindingDecision] = field(default_factory=list)
    decided_at: Optional[str] = None


@dataclass
class ReviewRequest:
    run_id: str
    client_id: str
    client_name: str
    reasons: list[str]
    risk: RiskScore
    findings: list[Anomaly]


# --- execution trace / final report -----------------------------------------

@dataclass
class StepRecord:
    step: str
    agent: str
    status: StepStatus
    started_at: str
    duration_ms: float
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class AdvisoryReport:
    run_id: str
    client_id: str
    client_name: str
    generated_at: str
    status: RunStatus
    as_of: Optional[str] = None
    profile: Optional[ClientProfile] = None
    portfolio: Optional[PortfolioSummary] = None
    cashflow: Optional[CashflowSummary] = None
    risk: Optional[RiskScore] = None
    anomalies: list[Anomaly] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    narrative: str = ""
    narrative_source: str = "template"
    review: dict = field(default_factory=dict)
    analysis_notes: list[str] = field(default_factory=list)
    data_quality: list[DataQualityIssue] = field(default_factory=list)
    degraded_sources: list[str] = field(default_factory=list)
    execution_trace: list[StepRecord] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return to_jsonable(self)
