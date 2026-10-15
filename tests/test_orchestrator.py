"""End-to-end flows through the orchestrator: happy path, HITL, memory, degradation, failures."""

from __future__ import annotations

import pytest

from wealth_advisor.errors import InvalidRunStateError, RunNotFoundError
from wealth_advisor.hitl import DeferredReviewer, StaticReviewer
from wealth_advisor.memory import FileSessionMemory, InMemoryLongTermMemory, SQLiteLongTermMemory
from wealth_advisor.models import FindingDecision, FindingStatus, ReviewDecision, RunStatus, Severity
from wealth_advisor.tools.base import FunctionTool, ToolSpec


def by_type(report):
    return {a.type: a for a in report.anomalies}


def test_clean_client_completes_without_review(make_system):
    r = make_system(reviewer=DeferredReviewer()).orchestrator.run("C005")
    assert r.status == RunStatus.COMPLETED
    assert r.review == {"required": False}
    assert r.risk.band == "low"
    assert [s.step for s in r.execution_trace] == ["fetch", "analyze", "advise"]
    assert r.recommendations[0].category == "general"


def test_red_flags_detected_for_c002(make_system):
    r = make_system().orchestrator.run("C002")
    kinds = by_type(r)
    assert {"structuring", "large_outflow", "kyc_expired", "duplicate_charge"} <= set(kinds)
    assert kinds["structuring"].severity == Severity.CRITICAL
    assert r.recommendations[0].priority == "P1" and r.recommendations[0].requires_approval


def test_deferred_review_pauses_and_resumes(make_system):
    system = make_system(reviewer=DeferredReviewer())
    r = system.orchestrator.run("C002")
    assert r.status == RunStatus.AWAITING_REVIEW and r.recommendations == []
    pending = system.orchestrator.pending_review(r.run_id)
    assert pending and pending["findings"]

    dup = by_type(r)["duplicate_charge"].fingerprint
    final = system.orchestrator.resume(r.run_id, ReviewDecision(
        approved=True, reviewer="qa", finding_decisions=[FindingDecision(dup, "dismiss", note="legit")]))
    assert final.status == RunStatus.COMPLETED
    assert by_type(final)["duplicate_charge"].status == FindingStatus.DISMISSED
    assert final.risk.value < r.risk.value  # rescored after dismissal
    assert "rescore" in [s.step for s in final.execution_trace]
    with pytest.raises(InvalidRunStateError):
        system.orchestrator.resume(r.run_id, ReviewDecision(approved=True, reviewer="qa"))


def test_resume_unknown_run(make_system):
    with pytest.raises(RunNotFoundError):
        make_system().orchestrator.resume("nope", ReviewDecision(approved=True, reviewer="x"))


def test_dismissed_finding_is_suppressed_in_next_run(make_system):
    lt = InMemoryLongTermMemory()
    first = make_system(reviewer=DeferredReviewer(), long_term=lt)
    r = first.orchestrator.run("C002")
    dup = by_type(r)["duplicate_charge"].fingerprint
    first.orchestrator.resume(r.run_id, ReviewDecision(True, "qa", finding_decisions=[FindingDecision(dup, "dismiss")]))

    second = make_system(long_term=lt).orchestrator.run("C002")
    kinds = by_type(second)
    assert kinds["duplicate_charge"].status == FindingStatus.SUPPRESSED
    assert kinds["structuring"].status == FindingStatus.RECURRING
    assert second.risk.previous_value is not None and second.risk.delta is not None


def test_severity_override(make_system):
    system = make_system(reviewer=DeferredReviewer())
    r = system.orchestrator.run("C002")
    fp = by_type(r)["kyc_expired"].fingerprint
    final = system.orchestrator.resume(r.run_id, ReviewDecision(
        True, "qa", finding_decisions=[FindingDecision(fp, "override", severity=Severity.LOW)]))
    a = by_type(final)["kyc_expired"]
    assert a.severity == Severity.LOW and a.original_severity == Severity.HIGH


def test_rejected_run_has_no_advice_and_no_history(make_system):
    lt = InMemoryLongTermMemory()
    r = make_system(reviewer=StaticReviewer(ReviewDecision(approved=False, reviewer="qa")),
                    long_term=lt).orchestrator.run("C002")
    assert r.status == RunStatus.REJECTED and r.recommendations == []
    assert lt.history("C002") == []


def test_crm_outage_uses_cached_profile(make_system):
    lt = InMemoryLongTermMemory()
    make_system(long_term=lt).orchestrator.run("C002")           # warms the CRM snapshot cache
    r = make_system(crm="down", long_term=lt).orchestrator.run("C002")
    assert r.status == RunStatus.COMPLETED_WITH_WARNINGS
    assert r.profile.source == "cache" and r.profile.stale
    assert "crm" in r.degraded_sources
    assert "kyc_expired" in by_type(r)  # compliance checks still ran on cached data


def test_crm_outage_without_cache_degrades(make_system):
    r = make_system(crm="down").orchestrator.run("C001")
    assert r.status == RunStatus.COMPLETED_WITH_WARNINGS
    assert r.profile.source == "unavailable"
    kinds = by_type(r)
    assert "compliance_unverified" in kinds and "allocation_drift" not in kinds
    assert any("No risk profile" in n for n in r.analysis_notes)


def test_transient_crm_failure_recovers_via_retry(make_system):
    r = make_system(crm="transient").orchestrator.run("C001")
    assert r.profile.source == "crm" and "crm" not in r.degraded_sources


def test_market_data_outage_values_at_cost(make_system):
    r = make_system(market="down").orchestrator.run("C005")
    assert set(r.degraded_sources) >= {"market_data", "fx_rates"}
    assert set(r.portfolio.priced_with_fallback) == {"VTI", "VXUS", "BND", "GLD"}
    assert r.status == RunStatus.COMPLETED_WITH_WARNINGS


def test_dirty_data_client_is_processed_with_issues(make_system):
    r = make_system().orchestrator.run("C004")
    codes = {i.code for i in r.data_quality}
    assert {"invalid_amount", "invalid_timestamp", "unknown_currency", "duplicate_record", "invalid_holding",
            "unknown_balance", "client_not_in_crm", "unknown_ticker"} <= codes
    assert r.status == RunStatus.COMPLETED_WITH_WARNINGS
    assert r.portfolio.priced_with_fallback == ["PRIVCO"]


def test_missing_client_fails_gracefully(make_system):
    r = make_system().orchestrator.run("C999")
    assert r.status == RunStatus.FAILED and "C999" in r.error
    assert r.execution_trace[0].status.value == "failed"


def test_batch_isolates_failures(make_system):
    reports = make_system().orchestrator.run_batch(["C999", "C005"])
    assert [r.status for r in reports] == [RunStatus.FAILED, RunStatus.COMPLETED]


def test_detector_crash_degrades_instead_of_failing(make_system):
    system = make_system()
    reg = system.registry._tools["anomaly.scan_cashflow"]

    def boom(**kw):
        raise RuntimeError("detector bug")

    reg.tool = FunctionTool(ToolSpec("anomaly.scan_cashflow", "broken", reg.tool.spec.params), boom)
    r = system.orchestrator.run("C003")
    assert r.status == RunStatus.COMPLETED_WITH_WARNINGS
    analyze = next(s for s in r.execution_trace if s.step == "analyze")
    assert analyze.status.value == "degraded" and "cashflow_trends" in analyze.warnings[0]
    assert "velocity_burst" in by_type(r)  # other detectors still ran


def test_paused_run_can_be_resumed_by_a_new_process(make_system, tmp_path):
    lt = SQLiteLongTermMemory(tmp_path / "mem.db")
    s1 = make_system(reviewer=DeferredReviewer(), long_term=lt, session=FileSessionMemory(tmp_path / "runs"))
    r = s1.orchestrator.run("C003")
    assert r.status == RunStatus.AWAITING_REVIEW

    s2 = make_system(long_term=SQLiteLongTermMemory(tmp_path / "mem.db"), session=FileSessionMemory(tmp_path / "runs"))
    final = s2.orchestrator.resume(r.run_id, ReviewDecision(True, "qa"))
    assert final.status == RunStatus.COMPLETED and final.recommendations
    assert s2.long_term.history("C003")[0]["run_id"] == r.run_id
