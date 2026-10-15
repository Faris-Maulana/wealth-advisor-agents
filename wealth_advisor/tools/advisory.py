"""Map findings to prioritised, actionable recommendations.

Rules are explicit and auditable: each recommendation lists the fingerprints of
the findings that triggered it. Findings of the same family collapse into one
recommendation (e.g. five portfolio findings -> one rebalancing proposal) so
the advisor gets a short action list, not a mirror of the alert list.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..config import RISK_PROFILE_TARGETS
from ..models import (
    AnalysisResult,
    Anomaly,
    ClientProfile,
    Recommendation,
    Severity,
)
from .base import FunctionTool, ParamSpec, ToolSpec

FAMILY = {
    "structuring": "aml",
    "large_outflow": "transaction_verification",
    "new_counterparty": "transaction_verification",
    "unusual_geography": "transaction_verification",
    "velocity_burst": "transaction_verification",
    "amount_outlier": "transaction_verification",
    "duplicate_charge": "dispute",
    "concentration_position": "rebalance",
    "concentration_sector": "rebalance",
    "allocation_drift": "rebalance",
    "excess_volatility": "rebalance",
    "suitability_mismatch": "suitability",
    "risk_profile_stale": "suitability",
    "low_cash_buffer": "liquidity",
    "income_drop": "cashflow_plan",
    "spending_trend": "budget",
    "kyc_expired": "kyc",
    "kyc_not_verified": "kyc",
    "kyc_expiring": "kyc",
    "compliance_unverified": "kyc",
}

PRIORITY = {Severity.CRITICAL: "P1", Severity.HIGH: "P1", Severity.MEDIUM: "P2", Severity.LOW: "P3"}


def _max_sev(items: list[Anomaly]) -> Severity:
    return max((a.severity for a in items), key=lambda s: s.rank)


def _rebalance_action(items: list[Anomaly], profile: Optional[ClientProfile]) -> tuple[str, str]:
    trims = [a.evidence["ticker"] for a in items if a.type == "concentration_position"]
    rp = profile.risk_profile if profile else None
    parts = []
    if trims:
        parts.append(f"trim {', '.join(trims)} to below the single-name limit")
    if rp in RISK_PROFILE_TARGETS:
        tgt = RISK_PROFILE_TARGETS[rp]["allocation"]
        parts.append("rebalance toward the {} target ({})".format(
            rp, ", ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in tgt.items())))
    action = "Prepare a rebalancing proposal: " + "; ".join(parts) + \
        ". Use tax-lot selection to minimise realised gains." if parts else \
        "Review portfolio diversification with the client."
    rationale = "; ".join(a.title for a in items)
    return action, rationale


def build_recommendations(analysis: AnalysisResult, profile: Optional[ClientProfile]) -> list[Recommendation]:
    families: dict[str, list[Anomaly]] = defaultdict(list)
    for a in analysis.anomalies:
        if a.counts_toward_risk:
            families[FAMILY.get(a.type, "general")].append(a)

    recs: list[Recommendation] = []

    def add(category: str, items: list[Anomaly], action: str, rationale: str, approval: bool,
            priority: Optional[str] = None) -> None:
        recs.append(Recommendation(
            id=f"R{len(recs) + 1}", priority=priority or PRIORITY[_max_sev(items)], category=category,
            action=action, rationale=rationale, linked_findings=[a.fingerprint for a in items],
            requires_approval=approval))

    if items := families.get("aml"):
        add("compliance", items,
            "Escalate to the compliance/AML officer for review and possible suspicious-activity filing. "
            "Do not discuss the escalation with the client (tipping-off risk).",
            "; ".join(a.title for a in items), approval=True, priority="P1")
    if items := families.get("transaction_verification"):
        total = sum(abs(a.evidence.get("amount", 0) or a.evidence.get("total", 0)) for a in items)
        n_txns = len({tid for a in items for tid in a.transaction_ids})
        add("compliance", items,
            f"Contact the client through a verified channel to confirm {n_txns} flagged transaction(s) "
            f"(~${total:,.0f}). If unconfirmed, freeze outbound transfers and escalate to compliance.",
            "; ".join(a.title for a in items), approval=_max_sev(items) >= Severity.HIGH)
    if items := families.get("dispute"):
        add("client_service", items,
            "Ask the client to confirm and dispute the duplicate charge(s) with the merchant/card issuer.",
            "; ".join(a.title for a in items), approval=False)
    if items := families.get("kyc"):
        add("compliance", items,
            "Refresh KYC documentation before executing any new trades or transfers.",
            "; ".join(a.title for a in items), approval=False)
    if items := families.get("suitability"):
        add("suitability", items,
            "Schedule a risk-profile review meeting and re-run the suitability questionnaire before any "
            "rebalancing trades.",
            "; ".join(a.title for a in items), approval=False)
    if items := families.get("rebalance"):
        action, rationale = _rebalance_action(items, profile)
        add("portfolio", items, action, rationale, approval=True)
    if items := families.get("liquidity"):
        cf = analysis.cashflow
        target = 3 * (cf.avg_monthly_spend if cf else 0)
        gap = max(0.0, target - (cf.liquid_assets if cf else 0))
        add("financial_planning", items,
            f"Build the emergency fund to 3 months of spending (~${target:,.0f}; gap ~${gap:,.0f}). Pause "
            "discretionary investment contributions until the buffer is restored.",
            "; ".join(a.title for a in items), approval=False)
    if items := families.get("cashflow_plan"):
        add("financial_planning", items,
            "Reach out to understand the income change (job change / life event) and revise the cash-flow "
            "plan and contribution schedule accordingly.",
            "; ".join(a.title for a in items), approval=False)
    if items := families.get("budget"):
        add("financial_planning", items,
            "Review the discretionary budget with the client and confirm whether the increase is planned "
            "(e.g. travel) or structural.",
            "; ".join(a.title for a in items), approval=False)

    if not recs:
        recs.append(Recommendation(
            id="R1", priority="P3", category="general",
            action="No action required. Maintain the current plan and keep the regular review cadence.",
            rationale="No material findings in this run."))
    order = {"P1": 0, "P2": 1, "P3": 2}
    recs.sort(key=lambda r: order[r.priority])
    for i, r in enumerate(recs, 1):
        r.id = f"R{i}"
    return recs


RECOMMEND_TOOL = FunctionTool(
    ToolSpec("advisory.recommend", "Derive prioritised recommendations from analysis findings.",
             {"analysis": ParamSpec(AnalysisResult), "profile": ParamSpec(ClientProfile, required=False)}),
    build_recommendations,
)
