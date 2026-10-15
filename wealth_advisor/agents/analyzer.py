"""Analyzer Agent: financial analysis, anomaly detection and risk scoring.

Each analysis family runs independently; one failing detector degrades the
result instead of discarding everything else. Long-term memory is used to
label findings as new vs recurring and to suppress findings a reviewer has
already dismissed as false positives.
"""

from __future__ import annotations

from typing import Optional

from ..models import (
    AnalysisResult,
    Anomaly,
    ClientContext,
    FindingStatus,
    RiskScore,
    StepStatus,
    to_jsonable,
)
from ..tools.anomaly import derive_usual_countries
from ..tools.scoring import compute_risk_score
from .base import Agent, AgentResult, RunContext, Task


class AnalyzerAgent(Agent):
    name = "analyzer"
    capabilities = frozenset({"analyze_client", "rescore_analysis"})
    required_tools = frozenset({"portfolio.analyze", "cashflow.summarize", "anomaly.scan_transactions",
                                "anomaly.scan_cashflow", "anomaly.scan_portfolio", "compliance.check_profile",
                                "risk.score"})

    def execute(self, task: Task, ctx: RunContext) -> AgentResult:
        if task.type == "rescore_analysis":
            return self._rescore(task.payload["analysis"], ctx)
        return self._analyze(task.payload["context"], ctx)

    def _analyze(self, c: ClientContext, ctx: RunContext) -> AgentResult:
        th = ctx.settings.thresholds
        warnings: list[str] = []
        notes: list[str] = []
        failed: list[str] = []
        profile = c.profile
        risk_profile = profile.risk_profile if profile.source != "unavailable" else None

        pf = self.tools.call("portfolio.analyze", holdings=c.holdings, quotes=c.quotes, accounts=c.accounts,
                             risk_profile=risk_profile)
        portfolio = pf.data if pf.ok else None
        if not pf.ok:
            failed.append("portfolio")
        cf = self.tools.call("cashflow.summarize", transactions=c.transactions, accounts=c.accounts, as_of=c.as_of)
        cashflow = cf.data if cf.ok else None
        if not cf.ok:
            failed.append("cashflow")

        if profile.usual_countries:
            usual = profile.usual_countries
        else:
            usual = derive_usual_countries(c.transactions, c.home_country)
            self.decide("usual_countries_derived", countries=usual, reason="no CRM country list")

        anomalies: list[Anomaly] = []
        liquid = cashflow.liquid_assets if cashflow else 0.0
        scans = [
            ("transactions", "anomaly.scan_transactions",
             dict(transactions=c.transactions, liquid_assets=liquid, as_of=c.as_of,
                  home_country=c.home_country, usual_countries=usual, thresholds=th)),
            ("cashflow_trends", "anomaly.scan_cashflow",
             dict(transactions=c.transactions, cashflow=cashflow, as_of=c.as_of, thresholds=th) if cashflow else None),
            ("portfolio_risk", "anomaly.scan_portfolio",
             dict(portfolio=portfolio, risk_profile=risk_profile, thresholds=th) if portfolio else None),
            ("compliance", "compliance.check_profile",
             dict(profile=profile, portfolio=portfolio, as_of=c.as_of, thresholds=th)),
        ]
        for label, tool, kwargs in scans:
            if kwargs is None:
                self.decide("scan_skipped", scan=label, reason="prerequisite analysis unavailable")
                failed.append(label)
                continue
            res = self.tools.call(tool, **kwargs)
            if res.ok:
                anomalies.extend(res.data)
                self.log.info("scan.complete", scan=label, findings=len(res.data))
            else:
                failed.append(label)
        if risk_profile is None:
            notes.append("No risk profile available: allocation drift, volatility and suitability checks skipped.")
        if c.degraded_sources:
            notes.append("Degraded inputs: " + ", ".join(c.degraded_sources))

        self._apply_memory(anomalies, ctx)
        anomalies.sort(key=lambda a: (a.counts_toward_risk, a.severity.rank, a.score), reverse=True)

        previous = ctx.long_term.last_run_summary(ctx.client_id)
        risk = self._score(anomalies, previous.get("risk") if previous else None)
        if failed:
            warnings.append("analysis incomplete: " + ", ".join(failed))
        result = AnalysisResult(portfolio=portfolio, cashflow=cashflow, anomalies=anomalies, risk=risk, notes=notes)
        ctx.session.put(ctx.run_id, "analysis", to_jsonable(result))
        self.log.info("analysis.summary", findings=len(anomalies),
                      active=sum(a.counts_toward_risk for a in anomalies),
                      risk_score=risk.value, risk_band=risk.band, failed_scans=failed)
        status = StepStatus.DEGRADED if failed else StepStatus.OK
        return AgentResult(status, output=result, warnings=warnings)

    def _apply_memory(self, anomalies: list[Anomaly], ctx: RunContext) -> None:
        dismissed = ctx.long_term.dismissed_fingerprints(ctx.client_id)
        seen = ctx.long_term.seen_fingerprints(ctx.client_id)
        for a in anomalies:
            if a.fingerprint in dismissed:
                fb = dismissed[a.fingerprint]
                a.status = FindingStatus.SUPPRESSED
                a.reviewer_note = f"Previously dismissed by {fb['reviewer']}: {fb.get('note') or 'no note'}"
                self.decide("finding_suppressed", fingerprint=a.fingerprint, type=a.type, by=fb["reviewer"])
            elif a.fingerprint in seen:
                a.status = FindingStatus.RECURRING

    def _score(self, anomalies: list[Anomaly], previous: Optional[dict]) -> RiskScore:
        res = self.tools.call("risk.score", anomalies=anomalies, previous=previous)
        if res.ok:
            return res.data
        self.decide("risk_score_fallback", reason=res.error)
        return compute_risk_score(anomalies, previous)

    def _rescore(self, analysis: AnalysisResult, ctx: RunContext) -> AgentResult:
        previous = ctx.long_term.last_run_summary(ctx.client_id)
        analysis.risk = self._score(analysis.anomalies, previous.get("risk") if previous else None)
        ctx.session.put(ctx.run_id, "analysis", to_jsonable(analysis))
        self.log.info("analysis.rescored", risk_score=analysis.risk.value, risk_band=analysis.risk.band)
        return AgentResult(StepStatus.OK, output=analysis)
