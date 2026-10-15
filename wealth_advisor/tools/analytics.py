"""Portfolio and cash-flow analytics (pure functions, exposed as tools)."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from typing import Optional

from ..config import INCOME_CATEGORIES, NON_SPEND_CATEGORIES, RISK_PROFILE_TARGETS
from ..models import Account, CashflowSummary, Holding, PortfolioSummary, Quote, Transaction
from .base import FunctionTool, ParamSpec, ToolSpec

LIQUID_ACCOUNT_TYPES = {"checking", "savings"}
ASSET_CLASSES = ("equity", "fixed_income", "cash", "alternative")


def liquid_assets(accounts: list[Account]) -> float:
    return round(sum(a.balance for a in accounts if a.type in LIQUID_ACCOUNT_TYPES), 2)


def analyze_portfolio(holdings: list[Holding], quotes: dict[str, Quote], accounts: list[Account],
                      risk_profile: Optional[str] = None) -> PortfolioSummary:
    """Investable portfolio = holdings + brokerage cash. Checking/savings are liquidity, not allocation.

    Holdings without a quote are valued at cost basis (flagged) so the total is
    not silently understated; they are excluded from volatility because we
    have no risk estimate for them.
    """
    values: dict[str, float] = {}
    classes: dict[str, str] = {}
    sectors: dict[str, str] = {}
    vols: dict[str, Optional[float]] = {}
    fallback, pnl = [], 0.0
    for h in holdings:
        q = quotes.get(h.ticker)
        if q:
            values[h.ticker] = h.quantity * q.price
            classes[h.ticker], sectors[h.ticker], vols[h.ticker] = q.asset_class, q.sector, q.volatility
            pnl += values[h.ticker] - h.cost_basis
        else:
            values[h.ticker] = h.cost_basis
            classes[h.ticker], sectors[h.ticker], vols[h.ticker] = "alternative", "unknown", None
            fallback.append(h.ticker)

    brokerage_cash = sum(a.balance for a in accounts if a.type == "brokerage")
    invested = sum(values.values())
    total = invested + brokerage_cash

    alloc = {c: 0.0 for c in ASSET_CLASSES}
    sector_w: dict[str, float] = defaultdict(float)
    pos_w: dict[str, float] = {}
    if total > 0:
        for t, v in values.items():
            w = v / total
            pos_w[t] = round(w, 4)
            alloc[classes[t]] = alloc.get(classes[t], 0.0) + w
            sector_w[sectors[t]] += w
        alloc["cash"] += brokerage_cash / total

    priced_weight = sum(pos_w[t] for t, v in vols.items() if v is not None)
    cash_weight = brokerage_cash / total if total else 0.0
    denom = priced_weight + cash_weight
    weighted_vol = (sum(pos_w[t] * v for t, v in vols.items() if v is not None) / denom) if denom > 0 else None

    target = RISK_PROFILE_TARGETS.get(risk_profile or "", {}).get("allocation", {})
    return PortfolioSummary(
        total_market_value=round(total, 2),
        invested_value=round(invested, 2),
        cash_value=round(brokerage_cash, 2),
        allocation={k: round(v, 4) for k, v in alloc.items()},
        target_allocation=dict(target),
        sector_weights={k: round(v, 4) for k, v in sorted(sector_w.items(), key=lambda kv: -kv[1])},
        position_weights=dict(sorted(pos_w.items(), key=lambda kv: -kv[1])),
        position_sectors=sectors,
        unrealized_pnl=round(pnl, 2),
        weighted_volatility=round(weighted_vol, 4) if weighted_vol is not None else None,
        priced_with_fallback=fallback,
    )


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_range(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def monthly_series(txns: list[Transaction], months: list[str], *, income: bool,
                   categories: Optional[set[str]] = None) -> dict[str, float]:
    series = {m: 0.0 for m in months}
    for t in txns:
        k = month_key(t.timestamp.date())
        if k not in series:
            continue
        if income:
            if t.amount > 0 and t.category in INCOME_CATEGORIES:
                series[k] += t.amount
        elif t.amount < 0 and t.category not in NON_SPEND_CATEGORIES:
            if categories is None or t.category in categories:
                series[k] += -t.amount
    return {k: round(v, 2) for k, v in series.items()}


def summarize_cashflow(transactions: list[Transaction], accounts: list[Account], as_of: date) -> CashflowSummary:
    liquid = liquid_assets(accounts)
    if not transactions:
        return CashflowSummary([], {}, {}, 0.0, 0.0, None, {}, liquid, None, 0)
    months = month_range(transactions[0].timestamp.date(), as_of)
    inc = monthly_series(transactions, months, income=True)
    spend = monthly_series(transactions, months, income=False)
    avg_inc = statistics.fmean(inc.values())
    avg_spend = statistics.fmean(spend.values())
    # Median spend is used for the buffer so a single large one-off outflow
    # does not make the client look better-covered or worse-covered than usual.
    median_spend = statistics.median(spend.values())
    cats: dict[str, float] = defaultdict(float)
    for t in transactions:
        if t.amount < 0 and t.category not in NON_SPEND_CATEGORIES:
            cats[t.category] += -t.amount
    top = dict(sorted(((k, round(v, 2)) for k, v in cats.items()), key=lambda kv: -kv[1])[:6])
    return CashflowSummary(
        months=months,
        monthly_income=inc,
        monthly_spend=spend,
        avg_monthly_income=round(avg_inc, 2),
        avg_monthly_spend=round(avg_spend, 2),
        savings_rate=round((avg_inc - avg_spend) / avg_inc, 4) if avg_inc > 0 else None,
        top_spend_categories=top,
        liquid_assets=liquid,
        cash_buffer_months=round(liquid / median_spend, 2) if median_spend > 0 else None,
        transaction_count=len(transactions),
    )


PORTFOLIO_TOOL = FunctionTool(
    ToolSpec("portfolio.analyze", "Value holdings, compute allocation, sector/position weights and volatility.",
             {"holdings": ParamSpec(list), "quotes": ParamSpec(dict), "accounts": ParamSpec(list),
              "risk_profile": ParamSpec(str, required=False)}),
    analyze_portfolio,
)

CASHFLOW_TOOL = FunctionTool(
    ToolSpec("cashflow.summarize", "Monthly income/spend, savings rate, top categories and cash buffer.",
             {"transactions": ParamSpec(list), "accounts": ParamSpec(list), "as_of": ParamSpec(date)}),
    summarize_cashflow,
)
