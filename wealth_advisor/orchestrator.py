"""Orchestrator Agent: central coordinator.

Execution flow (deterministic state machine, not LLM-planned):

    fetch_client_context  -> DataFetcherAgent   (critical)
    analyze_client        -> AnalyzerAgent      (critical)
    [review checkpoint]   -> Reviewer           (conditional; may pause the run)
    rescore_analysis      -> AnalyzerAgent      (only if the reviewer changed findings)
    build_advice          -> AdvisorAgent       (non-critical: report still ships)
    finalize              -> persist to long-term memory, produce report

Routing is capability-based: the orchestrator dispatches a `Task` by type and
the router resolves which agent handles it, so agents can be added or replaced
without touching the flow. All intermediate state lives in session memory so a
run paused at the review checkpoint can be resumed later (or in another process
with `FileSessionMemory`).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .agents.base import Agent, AgentResult, RunContext, Task
from .config import Settings
from .errors import InvalidRunStateError
from .hitl import Reviewer
from .logging_setup import get_logger, log_context
from .memory import LongTermMemory, SessionMemory
from .models import (
    AdvisoryReport,
    AnalysisResult,
    ClientContext,
    ClientProfile,
    FindingStatus,
    ReviewDecision,
    ReviewRequest,
    RunStatus,
    Severity,
    StepRecord,
    StepStatus,
    from_dict,
    to_jsonable,
)
from .tools.scoring import BAND_ORDER

log = get_logger("orchestrator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class Orchestrator:
    name = "orchestrator"

    def __init__(self, agents: list[Agent], session: SessionMemory, long_term: LongTermMemory,
                 reviewer: Reviewer, settings: Settings):
        self.router: dict[str, Agent] = {}
        for agent in agents:
            for cap in agent.capabilities:
                if cap in self.router:
                    raise ValueError(f"capability '{cap}' claimed by both {self.router[cap].name} and {agent.name}")
                self.router[cap] = agent
        self.session = session
        self.long_term = long_term
        self.reviewer = reviewer
        self.settings = settings

    # --- public API -----------------------------------------------------------

    def run(self, client_id: str, run_id: Optional[str] = None) -> AdvisoryReport:
        run_id = run_id or new_run_id()
        self.session.create(run_id, client_id=client_id, status=RunStatus.RUNNING.value, steps=[],
                            started_at=_now())
        ctx = self._ctx(run_id, client_id)
        with log_context(run_id=run_id, client_id=client_id, agent=self.name):
            log.info("run.start")
            fetched = self._dispatch("fetch", Task("fetch_client_context"), ctx)
            if fetched.status == StepStatus.FAILED:
                return self._fail(ctx, f"data fetch failed: {fetched.error}")
            context: ClientContext = fetched.output

            analyzed = self._dispatch("analyze", Task("analyze_client", {"context": context}), ctx)
            if analyzed.status == StepStatus.FAILED:
                return self._fail(ctx, f"analysis failed: {analyzed.error}", context=context)
            analysis: AnalysisResult = analyzed.output
            return self._review_gate(ctx, context, analysis)

    def resume(self, run_id: str, decision: ReviewDecision) -> AdvisoryReport:
        status = self.session.get(run_id, "status")
        if status != RunStatus.AWAITING_REVIEW.value:
            raise InvalidRunStateError(f"run '{run_id}' is '{status}', not awaiting review")
        client_id = self.session.get(run_id, "client_id")
        ctx = self._ctx(run_id, client_id)
        context = from_dict(ClientContext, self.session.get(run_id, "context"))
        analysis = from_dict(AnalysisResult, self.session.get(run_id, "analysis"))
        with log_context(run_id=run_id, client_id=client_id, agent=self.name):
            log.info("run.resumed", reviewer=decision.reviewer)
            return self._apply_review(ctx, context, analysis, decision)

    def pending_review(self, run_id: str) -> Optional[dict]:
        if self.session.get(run_id, "status") != RunStatus.AWAITING_REVIEW.value:
            return None
        return self.session.get(run_id, "review_request")

    def run_batch(self, client_ids: list[str]) -> list[AdvisoryReport]:
        """Clients are independent: one client's failure never stops the batch."""
        reports = []
        for cid in client_ids:
            try:
                reports.append(self.run(cid))
            except Exception as exc:  # last-resort isolation; run() already handles expected failures
                log.error("batch.client_crashed", client_id=cid, error=repr(exc), exc_info=True)
                reports.append(AdvisoryReport(run_id="n/a", client_id=cid, client_name=cid, generated_at=_now(),
                                              status=RunStatus.FAILED, error=repr(exc)))
        return reports

    # --- flow -----------------------------------------------------------------

    def _ctx(self, run_id: str, client_id: str) -> RunContext:
        return RunContext(run_id, client_id, self.session, self.long_term, self.settings)

    def _dispatch(self, step: str, task: Task, ctx: RunContext) -> AgentResult:
        agent = self.router.get(task.type)
        started = _now()
        if agent is None:
            log.error("route.no_agent", task=task.type)
            result = AgentResult(StepStatus.FAILED, error=f"no agent registered for task '{task.type}'")
            agent_name = "-"
        else:
            log.info("route.dispatch", step=step, task=task.type, to=agent.name)
            result = agent.handle(task, ctx)
            agent_name = agent.name
        record = StepRecord(step=step, agent=agent_name, status=result.status, started_at=started,
                            duration_ms=result.duration_ms, warnings=result.warnings, error=result.error)
        steps = self.session.get(ctx.run_id, "steps", [])
        steps.append(to_jsonable(record))
        self.session.put(ctx.run_id, "steps", steps)
        return result

    def _review_reasons(self, analysis: AnalysisResult) -> list[str]:
        reasons = []
        threshold = Severity(self.settings.review_min_severity)
        serious = [a for a in analysis.anomalies if a.counts_toward_risk and a.severity >= threshold]
        if serious:
            reasons.append(f"{len(serious)} finding(s) at or above '{threshold.value}' severity")
        if BAND_ORDER.index(analysis.risk.band) >= BAND_ORDER.index(self.settings.review_min_risk_band):
            reasons.append(f"risk band '{analysis.risk.band}' at or above '{self.settings.review_min_risk_band}'")
        return reasons

    def _review_gate(self, ctx: RunContext, context: ClientContext, analysis: AnalysisResult) -> AdvisoryReport:
        reasons = self._review_reasons(analysis)
        if not reasons:
            log.info("review.not_required", risk_band=analysis.risk.band)
            self.session.put(ctx.run_id, "review", {"required": False})
            return self._advise_and_finalize(ctx, context, analysis)

        request = ReviewRequest(
            run_id=ctx.run_id, client_id=context.client_id, client_name=context.name, reasons=reasons,
            risk=analysis.risk, findings=[a for a in analysis.anomalies if a.counts_toward_risk])
        self.session.put(ctx.run_id, "review_request", to_jsonable(request))
        self.session.put(ctx.run_id, "status", RunStatus.AWAITING_REVIEW.value)
        self.session.checkpoint(ctx.run_id)
        log.info("review.requested", reasons=reasons, findings=len(request.findings))

        started = time.perf_counter()
        decision = self.reviewer.review(request)
        if decision is None:
            log.info("review.deferred")
            return self._report(ctx, context, analysis, RunStatus.AWAITING_REVIEW)
        log.info("review.received", reviewer=decision.reviewer, approved=decision.approved,
                 wait_ms=round((time.perf_counter() - started) * 1000, 1))
        return self._apply_review(ctx, context, analysis, decision)

    def _apply_review(self, ctx: RunContext, context: ClientContext, analysis: AnalysisResult,
                      decision: ReviewDecision) -> AdvisoryReport:
        by_fp = {a.fingerprint: a for a in analysis.anomalies}
        changed = False
        applied, ignored = [], []
        for fd in decision.finding_decisions:
            a = by_fp.get(fd.fingerprint)
            if a is None:
                ignored.append(fd.fingerprint)
                continue
            if fd.action == "dismiss":
                a.status, a.reviewer_note = FindingStatus.DISMISSED, fd.note
                changed = True
            elif fd.action == "override" and fd.severity is not None:
                a.original_severity, a.severity = a.severity, fd.severity
                a.status, a.reviewer_note = FindingStatus.CONFIRMED, fd.note
                changed = True
            else:
                a.status = FindingStatus.CONFIRMED
            self.long_term.record_feedback(ctx.client_id, fd.fingerprint, fd.action, decision.reviewer, fd.note)
            applied.append({"fingerprint": fd.fingerprint, "action": fd.action})
        if ignored:
            log.warning("review.unknown_fingerprints", fingerprints=ignored)
        log.info("review.applied", decisions=applied, approved=decision.approved)

        review_info = {"required": True, "decision": to_jsonable(decision), "applied": applied,
                       "ignored_fingerprints": ignored,
                       "reasons": (self.session.get(ctx.run_id, "review_request") or {}).get("reasons", [])}
        self.session.put(ctx.run_id, "review", review_info)

        if changed:
            rescored = self._dispatch("rescore", Task("rescore_analysis", {"analysis": analysis}), ctx)
            if rescored.status != StepStatus.FAILED:
                analysis = rescored.output

        if not decision.approved:
            log.warning("run.rejected_by_reviewer", reviewer=decision.reviewer)
            report = self._report(ctx, context, analysis, RunStatus.REJECTED)
            self._persist(ctx, report)
            return report
        return self._advise_and_finalize(ctx, context, analysis)

    def _advise_and_finalize(self, ctx: RunContext, context: ClientContext,
                             analysis: AnalysisResult) -> AdvisoryReport:
        advised = self._dispatch("advise", Task("build_advice", {"context": context, "analysis": analysis}), ctx)
        advice = advised.output if advised.status != StepStatus.FAILED else None
        steps = self.session.get(ctx.run_id, "steps", [])
        degraded = any(s["status"] in ("degraded", "failed") for s in steps)
        status = RunStatus.COMPLETED_WITH_WARNINGS if degraded else RunStatus.COMPLETED
        report = self._report(ctx, context, analysis, status, advice=advice,
                              error=None if advice else f"advice step failed: {advised.error}")
        self._persist(ctx, report)
        log.info("run.complete", status=status.value, risk_score=analysis.risk.value,
                 recommendations=len(report.recommendations))
        return report

    def _persist(self, ctx: RunContext, report: AdvisoryReport) -> None:
        if report.status != RunStatus.REJECTED and report.risk is not None:
            self.long_term.save_run_summary(ctx.client_id, ctx.run_id, {
                "status": report.status.value,
                "risk": {"value": report.risk.value, "band": report.risk.band},
                "fingerprints": [a.fingerprint for a in report.anomalies],
                "active_findings": sum(a.counts_toward_risk for a in report.anomalies),
                "top_findings": [a.title for a in report.anomalies if a.counts_toward_risk][:5],
            })
        self.session.put(ctx.run_id, "status", report.status.value)
        self.session.put(ctx.run_id, "report", report.to_dict())
        self.session.checkpoint(ctx.run_id)

    def _fail(self, ctx: RunContext, error: str, context: Optional[ClientContext] = None) -> AdvisoryReport:
        log.error("run.failed", error=error)
        report = AdvisoryReport(
            run_id=ctx.run_id, client_id=ctx.client_id, client_name=context.name if context else ctx.client_id,
            generated_at=_now(), status=RunStatus.FAILED, error=error,
            data_quality=context.data_quality if context else [],
            execution_trace=self._trace(ctx))
        self.session.put(ctx.run_id, "status", report.status.value)
        self.session.put(ctx.run_id, "report", report.to_dict())
        self.session.checkpoint(ctx.run_id)
        return report

    def _trace(self, ctx: RunContext) -> list[StepRecord]:
        return [from_dict(StepRecord, s) for s in self.session.get(ctx.run_id, "steps", [])]

    def _report(self, ctx: RunContext, context: ClientContext, analysis: AnalysisResult, status: RunStatus,
                advice: Optional[dict] = None, error: Optional[str] = None) -> AdvisoryReport:
        profile: Optional[ClientProfile] = context.profile
        review = self.session.get(ctx.run_id, "review") or {}
        if status == RunStatus.AWAITING_REVIEW:
            review = {"required": True, "pending": True,
                      "reasons": (self.session.get(ctx.run_id, "review_request") or {}).get("reasons", [])}
        return AdvisoryReport(
            run_id=ctx.run_id, client_id=context.client_id, client_name=context.name, generated_at=_now(),
            status=status, as_of=context.as_of.isoformat(), profile=profile,
            portfolio=analysis.portfolio, cashflow=analysis.cashflow, risk=analysis.risk,
            anomalies=analysis.anomalies,
            recommendations=advice["recommendations"] if advice else [],
            narrative=advice["narrative"] if advice else "",
            narrative_source=advice["narrative_source"] if advice else "none",
            review=review, analysis_notes=analysis.notes, data_quality=context.data_quality, degraded_sources=context.degraded_sources,
            execution_trace=self._trace(ctx), error=error,
        )
