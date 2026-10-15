"""Detectors: each one in isolation with synthetic data, plus correlation merging."""

from __future__ import annotations

from datetime import date

from conftest import txn

from wealth_advisor.config import AnomalyThresholds
from wealth_advisor.models import ClientProfile, PortfolioSummary, Severity
from wealth_advisor.tools import anomaly as A
from wealth_advisor.tools.analytics import summarize_cashflow
from wealth_advisor.tools.scoring import compute_risk_score

TH = AnomalyThresholds()


def baseline(n=20, start=0, amount=-80.0, category="shopping"):
    return [txn(start + i, amount - (i % 5) * 7, days=i, category=category, counterparty=f"Shop{i % 4}")
            for i in range(n)]


def test_amount_outlier_detected_with_robust_stats():
    txns = baseline() + [txn(99, -2400, days=25)]
    found = A.detect_amount_outliers(txns, TH, recurring=set())
    assert [f.transaction_ids for f in found] == [["T0099"]]
    assert found[0].evidence["modified_z"] > TH.modified_z


def test_amount_outlier_ignores_small_samples_and_recurring():
    few = baseline(n=3) + [txn(99, -2400, days=5)]
    assert A.detect_amount_outliers(few, TH, recurring=set()) == []
    txns = baseline() + [txn(99, -2400, days=25)]
    assert A.detect_amount_outliers(txns, TH, recurring={"T0099"}) == []


def test_recurring_payments_recognised():
    rent = [txn(i, -2000, days=30 * i, category="housing", counterparty="Landlord") for i in range(4)]
    assert A.recurring_txn_ids(rent) == {t.txn_id for t in rent}


def test_structuring_cluster():
    deps = [txn(i, 9_500 + i * 50, days=i * 1.5, category="cash_deposit", channel="cash") for i in range(4)]
    found = A.detect_structuring(deps + [txn(10, 12_000, days=1, channel="cash")], TH)
    assert len(found) == 1 and found[0].severity == Severity.CRITICAL
    assert len(found[0].transaction_ids) == 4


def test_structuring_requires_window():
    spread = [txn(i, 9_500, days=i * 10, channel="cash") for i in range(4)]
    assert A.detect_structuring(spread, TH) == []


def test_duplicate_charge_within_window_only():
    a, b = txn(1, -286.4, counterparty="Pharmacy"), txn(2, -286.4, days=2 / 1440, counterparty="Pharmacy")
    c = txn(3, -286.4, days=3, counterparty="Pharmacy")
    found = A.detect_duplicates([a, b, c], TH)
    assert len(found) == 1 and found[0].transaction_ids == ["T0001", "T0002"]


def test_velocity_burst():
    burst = [txn(i, -1000, days=i * 0.25, channel="p2p", counterparty=f"P{i}") for i in range(5)]
    found = A.detect_velocity(burst, liquid=4000, th=TH)
    assert len(found) == 1 and found[0].severity == Severity.HIGH


def test_correlated_signals_merge_and_escalate():
    t = txn(1, -48_000, days=100, category="transfer_out", channel="wire", country="CY", counterparty="Meridian")
    txns = baseline() + [t]
    found = A.scan_transactions(txns, liquid_assets=100_000, as_of=date(2026, 9, 30), home_country="US",
                                usual_countries=["US"], thresholds=TH)
    on_t = [f for f in found if f.transaction_ids == ["T0001"]]
    assert len(on_t) == 1, "one merged finding per transaction"
    signals = {s["type"] for s in on_t[0].evidence["corroborating_signals"]}
    assert {"new_counterparty", "unusual_geography"} <= signals
    assert on_t[0].severity == Severity.CRITICAL


def _months_of(spend_by_month, income_by_month):
    out, i = [], 0
    for m, (spend, inc) in enumerate(zip(spend_by_month, income_by_month)):
        out.append(txn(i, -spend, days=30 * m + 2, category="dining")); i += 1
        if inc:
            out.append(txn(i, inc, days=30 * m + 1, category="salary")); i += 1
    return out


def test_income_drop_and_spending_trend():
    txns = _months_of([500, 520, 480, 1500, 1600, 1550], [8000, 8000, 8000, 8000, 0, 500])
    cf = summarize_cashflow(txns, [], date(2026, 11, 30))
    kinds = {a.type for a in A.scan_cashflow(txns, cf, date(2026, 11, 30), TH)}
    assert {"income_drop", "spending_trend"} <= kinds


def test_spending_trend_needs_material_absolute_change():
    txns = _months_of([50, 50, 50, 120, 120, 120], [5000] * 6)
    cf = summarize_cashflow(txns, [], date(2026, 11, 30))
    assert "spending_trend" not in {a.type for a in A.scan_cashflow(txns, cf, date(2026, 11, 30), TH)}


def portfolio(weights, sectors, alloc, vol=0.1):
    return PortfolioSummary(total_market_value=100_000, invested_value=100_000, cash_value=0, allocation=alloc,
                            target_allocation={}, sector_weights={}, position_weights=weights,
                            position_sectors=sectors, unrealized_pnl=0, weighted_volatility=vol)


def test_concentration_ignores_diversified_funds():
    p = portfolio({"VTI": 0.7, "NVDA": 0.3}, {"VTI": "diversified", "NVDA": "technology"},
                  {"equity": 1.0, "fixed_income": 0, "cash": 0, "alternative": 0})
    found = [a for a in A.scan_portfolio(p, None, TH) if a.type == "concentration_position"]
    assert [a.evidence["ticker"] for a in found] == ["NVDA"]


def test_drift_volatility_and_suitability():
    p = portfolio({"VTI": 1.0}, {"VTI": "diversified"},
                  {"equity": 0.95, "fixed_income": 0.05, "cash": 0, "alternative": 0}, vol=0.3)
    kinds = {a.type for a in A.scan_portfolio(p, "conservative", TH)}
    assert {"allocation_drift", "excess_volatility"} <= kinds
    prof = ClientProfile("C", risk_profile="conservative", kyc_status="verified", kyc_expiry=date(2027, 1, 1))
    comp = A.scan_compliance(prof, p, date(2026, 9, 30), TH)
    assert [a.type for a in comp] == ["suitability_mismatch"]


def test_kyc_checks_and_unverified_profile():
    prof = ClientProfile("C", risk_profile="moderate", kyc_status="verified", kyc_expiry=date(2026, 9, 1),
                         risk_profile_updated=date(2024, 1, 1))
    kinds = {a.type for a in A.scan_compliance(prof, None, date(2026, 9, 30), TH)}
    assert kinds == {"kyc_expired", "risk_profile_stale"}
    unavailable = A.scan_compliance(ClientProfile("C", source="unavailable"), None, date(2026, 9, 30), TH)
    assert [a.type for a in unavailable] == ["compliance_unverified"]


def test_risk_score_bounded_monotonic_and_excludes_dismissed():
    from wealth_advisor.models import Anomaly, FindingStatus
    mk = lambda sev: Anomaly("x", "transaction", sev, 1.0, "t", "d", {"key": [sev.value, id(object())]})  # noqa: E731
    assert compute_risk_score([]).value == 0
    one = compute_risk_score([mk(Severity.HIGH)]).value
    two = compute_risk_score([mk(Severity.HIGH), mk(Severity.MEDIUM)]).value
    assert 0 < one < two < 1
    many = compute_risk_score([mk(Severity.CRITICAL) for _ in range(20)])
    assert many.value <= 1 and many.band == "high"
    dismissed = mk(Severity.CRITICAL)
    dismissed.status = FindingStatus.DISMISSED
    assert compute_risk_score([dismissed]).value == 0
