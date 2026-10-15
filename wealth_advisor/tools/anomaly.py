"""Anomaly and risk-signal detection.

Design choices
--------------
* Deterministic, explainable detectors (robust statistics + rules) rather than a
  black-box model: with ~6 months of history per client there is not enough
  data to train anything meaningful, and advisors/compliance need to see *why*
  something was flagged. Every finding carries its evidence.
* Robust statistics: modified z-score on median/MAD, so the very outliers we
  are hunting do not inflate the baseline (mean/stdev would mask them).
* Recurring payments (rent, insurance premiums) are recognised and excluded
  from outlier baselines, which removes the most common false positive.
* Correlated signals on the same transaction are merged into one finding
  (with corroborating signals listed) to limit alert fatigue; agreement of
  three or more independent signals escalates severity by one level.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from ..config import (
    INCOME_CATEGORIES,
    NON_SPEND_CATEGORIES,
    RISK_PROFILE_TARGETS,
    AnomalyThresholds,
)
from ..models import (
    Anomaly,
    CashflowSummary,
    ClientProfile,
    PortfolioSummary,
    Severity,
    Transaction,
    make_fingerprint,
)
from .analytics import month_range, monthly_series
from .base import FunctionTool, ParamSpec, ToolSpec

DISCRETIONARY = {"dining", "shopping", "travel", "entertainment"}
FUND_SECTORS = {"diversified", "bonds"}
SEVERITY_ORDER = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


def _fmt(x: float) -> str:
    return f"${x:,.0f}"


def _bump(sev: Severity) -> Severity:
    return SEVERITY_ORDER[min(sev.rank + 1, len(SEVERITY_ORDER) - 1)]


def _txn_anomaly(kind: str, sev: Severity, score: float, title: str, desc: str, t: Transaction,
                 **evidence) -> Anomaly:
    ev = {"key": ["txn", t.txn_id], "amount": round(t.amount, 2), "counterparty": t.counterparty,
          "date": t.timestamp.isoformat(), "channel": t.channel, "country": t.country, **evidence}
    return Anomaly(kind, "transaction", sev, round(score, 3), title, desc, ev, [t.txn_id])


def recurring_txn_ids(txns: list[Transaction], min_months: int = 3, tolerance: float = 0.10) -> set[str]:
    """Outflows that repeat to the same counterparty with a stable amount across months."""
    by_cp: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.amount < 0:
            by_cp[t.counterparty.lower()].append(t)
    out: set[str] = set()
    for items in by_cp.values():
        if len(items) < min_months:
            continue
        med = statistics.median(-t.amount for t in items)
        stable = [t for t in items if med > 0 and abs(-t.amount - med) / med <= tolerance]
        if len({(t.timestamp.year, t.timestamp.month) for t in stable}) >= min_months:
            out.update(t.txn_id for t in stable)
    return out


# --- transaction-level detectors ---------------------------------------------

def detect_amount_outliers(txns: list[Transaction], th: AnomalyThresholds, recurring: set[str]) -> list[Anomaly]:
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.amount < 0 and t.category not in NON_SPEND_CATEGORIES and t.txn_id not in recurring:
            groups[t.category].append(t)
    out = []
    for cat, items in groups.items():
        if len(items) < th.min_category_samples:
            continue
        amounts = [-t.amount for t in items]
        med = statistics.median(amounts)
        mad = statistics.median(abs(a - med) for a in amounts)
        if mad > 0:
            scale = mad / 0.6745
        else:  # >50% identical values: fall back to mean absolute deviation
            meanad = statistics.fmean(abs(a - med) for a in amounts)
            if meanad == 0:
                continue
            scale = 1.253314 * meanad
        for t in items:
            z = (-t.amount - med) / scale
            if z > th.modified_z and -t.amount >= th.min_outlier_amount:
                sev = Severity.HIGH if z >= th.modified_z_high else Severity.MEDIUM
                out.append(_txn_anomaly(
                    "amount_outlier", sev, min(1.0, max(0.5, z / (th.modified_z_high * 1.5))),
                    f"Unusually large {cat} payment to {t.counterparty}",
                    f"{_fmt(-t.amount)} vs a typical {cat} payment of {_fmt(med)} (modified z-score {z:.1f}).",
                    t, modified_z=round(z, 2), category_median=round(med, 2), category_samples=len(items)))
    return out


def detect_large_outflows(txns: list[Transaction], liquid: float, th: AnomalyThresholds,
                          recurring: set[str]) -> list[Anomaly]:
    if liquid <= 0:
        return []
    out = []
    for t in txns:
        if t.amount >= 0 or t.category in NON_SPEND_CATEGORIES or t.txn_id in recurring:
            continue
        amt = -t.amount
        pct = amt / liquid
        if amt >= th.large_outflow_min_amount and pct >= th.large_outflow_pct_liquid:
            sev = Severity.CRITICAL if pct >= th.large_outflow_pct_liquid_critical else Severity.HIGH
            out.append(_txn_anomaly(
                "large_outflow", sev, min(1.0, 0.5 + pct),
                f"Large outflow: {_fmt(amt)} to {t.counterparty}",
                f"Single payment equal to {pct:.0%} of current liquid assets ({_fmt(liquid)}).",
                t, pct_of_liquid=round(pct, 4)))
    return out


def detect_structuring(txns: list[Transaction], th: AnomalyThresholds) -> list[Anomaly]:
    lo = th.structuring_threshold * (1 - th.structuring_band_pct)
    cands = [t for t in txns if t.amount > 0 and t.channel == "cash" and lo <= t.amount < th.structuring_threshold]
    out, used = [], set()
    window = timedelta(days=th.structuring_window_days)
    for i, start in enumerate(cands):
        if start.txn_id in used:
            continue
        cluster = [t for t in cands[i:] if t.timestamp - start.timestamp <= window and t.txn_id not in used]
        if len(cluster) >= th.structuring_min_count:
            used.update(t.txn_id for t in cluster)
            total = sum(t.amount for t in cluster)
            out.append(Anomaly(
                "structuring", "transaction", Severity.CRITICAL, 0.9,
                f"Possible structuring: {len(cluster)} cash deposits just under {_fmt(th.structuring_threshold)}",
                f"{len(cluster)} cash deposits totalling {_fmt(total)} within {th.structuring_window_days} days, each "
                f"between {_fmt(lo)} and {_fmt(th.structuring_threshold)}. Pattern consistent with avoiding "
                "currency-transaction reporting; requires compliance review.",
                {"key": ["structuring", [t.txn_id for t in cluster]], "total": round(total, 2),
                 "amounts": [t.amount for t in cluster],
                 "first": cluster[0].timestamp.isoformat(), "last": cluster[-1].timestamp.isoformat()},
                [t.txn_id for t in cluster]))
    return out


def detect_duplicates(txns: list[Transaction], th: AnomalyThresholds) -> list[Anomaly]:
    groups: dict[tuple, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.amount < 0:
            groups[(t.counterparty.lower(), round(t.amount, 2))].append(t)
    out = []
    window = timedelta(minutes=th.duplicate_window_minutes)
    for items in groups.values():
        for a, b in zip(items, items[1:]):
            if b.timestamp - a.timestamp <= window:
                gap = (b.timestamp - a.timestamp).total_seconds()
                out.append(Anomaly(
                    "duplicate_charge", "transaction", Severity.MEDIUM, 0.7,
                    f"Possible duplicate charge at {a.counterparty}",
                    f"Two identical charges of {_fmt(-a.amount)} {gap / 60:.1f} minutes apart.",
                    {"key": ["dup", a.txn_id, b.txn_id], "amount": a.amount, "counterparty": a.counterparty,
                     "seconds_apart": gap},
                    [a.txn_id, b.txn_id]))
    return out


def detect_new_counterparty(txns: list[Transaction], as_of: date, home: str, th: AnomalyThresholds) -> list[Anomaly]:
    outflows = [t for t in txns if t.amount < 0 and t.category not in NON_SPEND_CATEGORIES]
    if len(outflows) < 10:
        return []
    amounts = sorted(-t.amount for t in outflows)
    p_idx = min(len(amounts) - 1, int(th.new_counterparty_percentile * len(amounts)))
    cutoff = max(amounts[p_idx], th.new_counterparty_min_amount)
    window_start = as_of - timedelta(days=th.new_counterparty_lookback_days)
    first_seen: dict[str, date] = {}
    for t in txns:
        first_seen.setdefault(t.counterparty.lower(), t.timestamp.date())
    out = []
    for t in outflows:
        if first_seen[t.counterparty.lower()] >= window_start and -t.amount >= cutoff:
            international = t.country != home
            sev = Severity.HIGH if (t.channel == "wire" or international) else Severity.MEDIUM
            out.append(_txn_anomaly(
                "new_counterparty", sev, 0.65,
                f"High-value payment to new counterparty {t.counterparty}",
                f"First-ever payment to this counterparty ({_fmt(-t.amount)}), above the client's "
                f"{int(th.new_counterparty_percentile * 100)}th percentile outflow ({_fmt(cutoff)})"
                + (f", sent to {t.country}." if international else "."),
                t, percentile_cutoff=round(cutoff, 2)))
    return out


def detect_unusual_geography(txns: list[Transaction], usual: set[str], th: AnomalyThresholds) -> list[Anomaly]:
    out = []
    for t in txns:
        if t.amount < 0 and t.country not in usual and -t.amount >= th.unusual_country_min_amount:
            sev = Severity.HIGH if t.channel == "wire" else Severity.MEDIUM
            out.append(_txn_anomaly(
                "unusual_geography", sev, 0.6,
                f"Payment to unusual jurisdiction ({t.country})",
                f"{_fmt(-t.amount)} to {t.counterparty} in {t.country}; client's usual countries: "
                f"{', '.join(sorted(usual))}.",
                t, usual_countries=sorted(usual)))
    return out


def detect_velocity(txns: list[Transaction], liquid: float, th: AnomalyThresholds) -> list[Anomaly]:
    cands = [t for t in txns if t.amount < 0 and t.channel in th.velocity_channels]
    window = timedelta(hours=th.velocity_window_hours)
    out, used = [], set()
    for i, start in enumerate(cands):
        if start.txn_id in used:
            continue
        burst = [t for t in cands[i:] if t.timestamp - start.timestamp <= window]
        if len(burst) >= th.velocity_min_count:
            used.update(t.txn_id for t in burst)
            total = -sum(t.amount for t in burst)
            share = total / liquid if liquid > 0 else 1.0
            sev = Severity.HIGH if share >= 0.5 else Severity.MEDIUM
            out.append(Anomaly(
                "velocity_burst", "transaction", sev, min(1.0, 0.5 + share / 2),
                f"Burst of {len(burst)} outbound transfers in {th.velocity_window_hours}h",
                f"{len(burst)} transfers totalling {_fmt(total)} to {len({t.counterparty for t in burst})} "
                f"counterparties ({share:.0%} of liquid assets). Possible scam/account-takeover pattern.",
                {"key": ["velocity", [t.txn_id for t in burst]], "total": round(total, 2),
                 "counterparties": sorted({t.counterparty for t in burst}), "pct_of_liquid": round(share, 4)},
                [t.txn_id for t in burst]))
    return out


def merge_correlated(findings: list[Anomaly]) -> list[Anomaly]:
    """Merge single-transaction findings that point at the same transaction."""
    by_txn: dict[str, list[Anomaly]] = defaultdict(list)
    others: list[Anomaly] = []
    for f in findings:
        if f.category == "transaction" and len(f.transaction_ids) == 1:
            by_txn[f.transaction_ids[0]].append(f)
        else:
            others.append(f)
    merged = []
    for txn_id, group in by_txn.items():
        group.sort(key=lambda a: (a.severity.rank, a.score), reverse=True)
        primary = group[0]
        if len(group) > 1:
            primary.evidence["corroborating_signals"] = [
                {"type": g.type, "severity": g.severity.value, "detail": g.description} for g in group[1:]]
            combined = 1.0
            for g in group:
                combined *= 1 - g.score
            primary.score = round(1 - combined, 3)
            if len(group) >= 3:
                primary.severity = _bump(primary.severity)
            primary.description += f" Corroborated by: {', '.join(g.type for g in group[1:])}."
        merged.append(primary)
    # Single-transaction signals on a transaction that is already part of a
    # multi-transaction pattern (structuring, velocity burst) are folded into
    # that pattern as corroboration rather than reported separately.
    pattern_of = {tid: f for f in others for tid in f.transaction_ids}
    kept = []
    for m in merged:
        pattern = pattern_of.get(m.transaction_ids[0])
        if pattern is None:
            kept.append(m)
            continue
        if m.type != "amount_outlier":
            pattern.evidence.setdefault("corroborating_signals", []).append(
                {"type": m.type, "severity": m.severity.value, "detail": m.title})
    merged = kept
    return others + merged


def scan_transactions(transactions: list[Transaction], liquid_assets: float, as_of: date, home_country: str,
                      usual_countries: list, thresholds: AnomalyThresholds) -> list[Anomaly]:
    th = thresholds
    recurring = recurring_txn_ids(transactions)
    usual = set(usual_countries) | {home_country}
    findings = (
        detect_amount_outliers(transactions, th, recurring)
        + detect_large_outflows(transactions, liquid_assets, th, recurring)
        + detect_structuring(transactions, th)
        + detect_duplicates(transactions, th)
        + detect_new_counterparty(transactions, as_of, home_country, th)
        + detect_unusual_geography(transactions, usual, th)
        + detect_velocity(transactions, liquid_assets, th)
    )
    return merge_correlated(findings)


def derive_usual_countries(transactions: list[Transaction], home: str, min_count: int = 3) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for t in transactions:
        counts[t.country] += 1
    return sorted({c for c, n in counts.items() if n >= min_count} | {home})


# --- cash-flow trend detectors ------------------------------------------------

def scan_cashflow(transactions: list[Transaction], cashflow: CashflowSummary, as_of: date,
                  thresholds: AnomalyThresholds) -> list[Anomaly]:
    th = thresholds
    out: list[Anomaly] = []
    if not transactions:
        return out
    months = month_range(transactions[0].timestamp.date(), as_of)
    if len(months) >= th.min_months_for_trend:
        disc = monthly_series(transactions, months, income=False, categories=DISCRETIONARY)
        vals = [disc[m] for m in months]
        half = min(3, len(vals) // 2)
        # Medians, not means: one large trip should not read as a structural trend.
        prior, recent = statistics.median(vals[-2 * half:-half]), statistics.median(vals[-half:])
        if prior > 0:
            change = (recent - prior) / prior
            if change >= th.spending_trend_pct and recent - prior >= th.spending_trend_min_abs:
                sev = Severity.HIGH if change >= 2 * th.spending_trend_pct else Severity.MEDIUM
                out.append(Anomaly(
                    "spending_trend", "cashflow", sev, min(1.0, 0.4 + change / 2),
                    f"Discretionary spending up {change:.0%}",
                    f"Median monthly discretionary spend (dining, shopping, travel, entertainment) rose from "
                    f"{_fmt(prior)} to {_fmt(recent)} over the last {half} months.",
                    {"key": ["spending_trend", months[-1]], "prior_median": round(prior, 2),
                     "recent_median": round(recent, 2), "change_pct": round(change, 4),
                     "monthly": {m: disc[m] for m in months}}))

        inc = cashflow.monthly_income
        series = [inc.get(m, 0.0) for m in months]
        baseline_vals = series[:-2]
        if len(baseline_vals) >= 2:
            baseline = statistics.median(baseline_vals)
            recent_inc = statistics.fmean(series[-2:])
            if baseline > 0:
                drop = (baseline - recent_inc) / baseline
                if drop >= th.income_drop_pct:
                    out.append(Anomaly(
                        "income_drop", "cashflow", Severity.HIGH, min(1.0, 0.5 + drop / 2),
                        f"Income down {drop:.0%} vs baseline",
                        f"Recurring income averaged {_fmt(recent_inc)}/month over the last 2 months vs a "
                        f"baseline of {_fmt(baseline)}/month. Possible job change or life event.",
                        {"key": ["income_drop", months[-1]], "baseline": round(baseline, 2),
                         "recent_avg": round(recent_inc, 2), "drop_pct": round(drop, 4),
                         "income_categories": sorted(INCOME_CATEGORIES)}))

    buf = cashflow.cash_buffer_months
    if buf is not None and buf < th.min_cash_buffer_months:
        sev = Severity.HIGH if buf < th.critical_cash_buffer_months else Severity.MEDIUM
        out.append(Anomaly(
            "low_cash_buffer", "cashflow", sev, 0.7,
            f"Cash buffer only {buf:.1f} months of spending",
            f"Liquid assets of {_fmt(cashflow.liquid_assets)} cover {buf:.1f} months of typical spend; "
            f"target is at least {th.min_cash_buffer_months:.0f} months.",
            {"key": ["low_cash_buffer"], "months": buf, "liquid_assets": cashflow.liquid_assets,
             "target_months": th.min_cash_buffer_months}))
    return out


# --- portfolio & compliance detectors ----------------------------------------

def implied_risk_profile(allocation: dict[str, float]) -> str:
    growth = allocation.get("equity", 0) + allocation.get("alternative", 0)
    if growth > 0.75:
        return "aggressive"
    if growth > 0.45:
        return "moderate"
    return "conservative"


PROFILE_RANK = {"conservative": 0, "moderate": 1, "aggressive": 2}


def scan_portfolio(portfolio: PortfolioSummary, risk_profile: Optional[str],
                   thresholds: AnomalyThresholds) -> list[Anomaly]:
    th = thresholds
    out: list[Anomaly] = []
    if portfolio.total_market_value <= 0:
        return out
    for ticker, w in portfolio.position_weights.items():
        sector = portfolio.position_sectors.get(ticker, "unknown")
        # Concentration applies to single-name exposure, not diversified funds.
        if sector in FUND_SECTORS:
            continue
        if w >= th.concentration_single:
            sev = Severity.HIGH if w >= th.concentration_single_high else Severity.MEDIUM
            out.append(Anomaly(
                "concentration_position", "portfolio", sev, min(1.0, w * 2),
                f"{ticker} is {w:.0%} of the portfolio",
                f"Single-name position above the {th.concentration_single:.0%} concentration limit"
                + (" (valued at cost; no market quote)." if ticker in portfolio.priced_with_fallback else "."),
                {"key": ["conc", ticker], "ticker": ticker, "weight": w, "limit": th.concentration_single}))
    for sector, w in portfolio.sector_weights.items():
        if sector in FUND_SECTORS or sector == "unknown":
            continue
        if w >= th.concentration_sector:
            out.append(Anomaly(
                "concentration_sector", "portfolio", Severity.MEDIUM, min(1.0, w * 1.5),
                f"{sector.replace('_', ' ').title()} sector is {w:.0%} of the portfolio",
                f"Sector exposure above the {th.concentration_sector:.0%} limit.",
                {"key": ["sector", sector], "sector": sector, "weight": w, "limit": th.concentration_sector}))

    if risk_profile in RISK_PROFILE_TARGETS:
        target = RISK_PROFILE_TARGETS[risk_profile]
        drifts = {c: round(portfolio.allocation.get(c, 0) - tw, 4) for c, tw in target["allocation"].items()}
        worst = max(drifts, key=lambda c: abs(drifts[c]))
        if abs(drifts[worst]) >= th.allocation_drift:
            sev = Severity.HIGH if abs(drifts[worst]) >= th.allocation_drift_high else Severity.MEDIUM
            out.append(Anomaly(
                "allocation_drift", "portfolio", sev, min(1.0, abs(drifts[worst]) * 3),
                f"Allocation drift: {worst.replace('_', ' ')} {drifts[worst]:+.0%} vs {risk_profile} target",
                f"{worst.replace('_', ' ').title()} is {portfolio.allocation.get(worst, 0):.0%} vs a target of "
                f"{target['allocation'][worst]:.0%} for a {risk_profile} profile.",
                {"key": ["drift", risk_profile], "drifts": drifts, "actual": portfolio.allocation,
                 "target": target["allocation"]}))
        vol = portfolio.weighted_volatility
        if vol is not None and vol > target["max_volatility"]:
            ratio = vol / target["max_volatility"]
            sev = Severity.HIGH if ratio >= 1.5 else Severity.MEDIUM
            out.append(Anomaly(
                "excess_volatility", "portfolio", sev, min(1.0, ratio / 3),
                f"Portfolio volatility {vol:.0%} exceeds {risk_profile} ceiling",
                f"Weighted volatility of {vol:.0%} vs a {target['max_volatility']:.0%} ceiling "
                "(upper-bound estimate assuming perfect correlation).",
                {"key": ["vol", risk_profile], "volatility": vol, "ceiling": target["max_volatility"]}))
    return out


def scan_compliance(profile: ClientProfile, portfolio: Optional[PortfolioSummary], as_of: date,
                    thresholds: AnomalyThresholds) -> list[Anomaly]:
    th = thresholds
    out: list[Anomaly] = []
    if profile.source == "unavailable":
        out.append(Anomaly(
            "compliance_unverified", "compliance", Severity.MEDIUM, 0.5,
            "KYC and risk profile could not be verified",
            "CRM data was unavailable and no cached profile exists; suitability and KYC checks were skipped.",
            {"key": ["compliance_unverified"]}))
        return out
    stale_note = " (from cached CRM snapshot)" if profile.stale else ""
    if profile.kyc_status != "verified":
        out.append(Anomaly(
            "kyc_not_verified", "compliance", Severity.HIGH, 0.8, "KYC not verified",
            f"KYC status is '{profile.kyc_status}'{stale_note}.", {"key": ["kyc_status", profile.kyc_status]}))
    if profile.kyc_expiry:
        days = (profile.kyc_expiry - as_of).days
        if days < 0:
            out.append(Anomaly(
                "kyc_expired", "compliance", Severity.HIGH, 0.85, f"KYC expired {-days} days ago",
                f"KYC documentation expired on {profile.kyc_expiry.isoformat()}{stale_note}; refresh before "
                "executing any transactions.",
                {"key": ["kyc_expired", profile.kyc_expiry.isoformat()], "expiry": profile.kyc_expiry.isoformat()}))
        elif days <= th.kyc_expiry_warning_days:
            out.append(Anomaly(
                "kyc_expiring", "compliance", Severity.LOW, 0.4, f"KYC expires in {days} days",
                f"KYC documentation expires on {profile.kyc_expiry.isoformat()}.",
                {"key": ["kyc_expiring", profile.kyc_expiry.isoformat()]}))
    if profile.risk_profile_updated:
        age = (as_of - profile.risk_profile_updated).days
        if age > th.risk_profile_max_age_days:
            out.append(Anomaly(
                "risk_profile_stale", "compliance", Severity.MEDIUM, 0.6,
                f"Risk profile last reviewed {age} days ago",
                f"Suitability questionnaire dated {profile.risk_profile_updated.isoformat()} is older than "
                f"{th.risk_profile_max_age_days} days.",
                {"key": ["rp_stale", profile.risk_profile_updated.isoformat()], "age_days": age}))
    if portfolio and profile.risk_profile in PROFILE_RANK and portfolio.total_market_value > 0:
        implied = implied_risk_profile(portfolio.allocation)
        if PROFILE_RANK[implied] > PROFILE_RANK[profile.risk_profile]:
            growth = portfolio.allocation.get("equity", 0) + portfolio.allocation.get("alternative", 0)
            out.append(Anomaly(
                "suitability_mismatch", "compliance", Severity.HIGH, 0.8,
                f"Portfolio is {implied} but client profile is {profile.risk_profile}",
                f"Growth assets are {growth:.0%} of the portfolio, consistent with an {implied} profile; "
                f"the documented risk tolerance is {profile.risk_profile}.",
                {"key": ["suitability", profile.risk_profile, implied], "stated": profile.risk_profile,
                 "implied": implied, "growth_weight": round(growth, 4)}))
    return out


TXN_SCAN_TOOL = FunctionTool(
    ToolSpec("anomaly.scan_transactions", "Detect unusual transactions (outliers, structuring, duplicates, "
             "new counterparties, geography, velocity).",
             {"transactions": ParamSpec(list), "liquid_assets": ParamSpec((int, float)), "as_of": ParamSpec(date),
              "home_country": ParamSpec(str), "usual_countries": ParamSpec(list),
              "thresholds": ParamSpec(AnomalyThresholds)}),
    scan_transactions,
)
CASHFLOW_SCAN_TOOL = FunctionTool(
    ToolSpec("anomaly.scan_cashflow", "Detect spending/income trends and liquidity shortfalls.",
             {"transactions": ParamSpec(list), "cashflow": ParamSpec(CashflowSummary), "as_of": ParamSpec(date),
              "thresholds": ParamSpec(AnomalyThresholds)}),
    scan_cashflow,
)
PORTFOLIO_SCAN_TOOL = FunctionTool(
    ToolSpec("anomaly.scan_portfolio", "Detect concentration, allocation drift and excess volatility.",
             {"portfolio": ParamSpec(PortfolioSummary), "risk_profile": ParamSpec(str, required=False),
              "thresholds": ParamSpec(AnomalyThresholds)}),
    scan_portfolio,
)
COMPLIANCE_SCAN_TOOL = FunctionTool(
    ToolSpec("compliance.check_profile", "KYC validity, risk-profile freshness and suitability checks.",
             {"profile": ParamSpec(ClientProfile), "portfolio": ParamSpec(PortfolioSummary, required=False),
              "as_of": ParamSpec(date), "thresholds": ParamSpec(AnomalyThresholds)}),
    scan_compliance,
)

__all__ = ["scan_transactions", "scan_cashflow", "scan_portfolio", "scan_compliance", "derive_usual_countries",
           "implied_risk_profile", "make_fingerprint"]
