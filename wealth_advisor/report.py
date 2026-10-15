"""Render an `AdvisoryReport` as Markdown for humans (JSON is the machine format)."""

from __future__ import annotations

from .models import AdvisoryReport, FindingStatus

SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _usd(x) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def render_markdown(r: AdvisoryReport) -> str:
    out: list[str] = []
    w = out.append
    w(f"# Advisory Report — {r.client_name} ({r.client_id})")
    w("")
    w(f"- **Run:** `{r.run_id}`  ")
    w(f"- **Status:** `{r.status.value}`  ")
    w(f"- **Data as of:** {r.as_of or 'n/a'} · generated {r.generated_at}")
    if r.error:
        w(f"- **Error:** {r.error}")
    if r.degraded_sources:
        w(f"- **Degraded sources:** {', '.join(r.degraded_sources)}")
    w("")

    if r.risk:
        delta = ""
        if r.risk.delta is not None:
            delta = f" (prev {r.risk.previous_value:.2f} {r.risk.previous_band}, Δ {r.risk.delta:+.2f})"
        w(f"## Risk: **{r.risk.band.upper()}** — {r.risk.value:.2f}{delta}")
        for d in r.risk.drivers:
            w(f"- {d}")
        w("")

    if r.narrative:
        w("## Summary")
        w(f"> {r.narrative}")
        w(f"<sub>narrative source: {r.narrative_source}</sub>")
        w("")

    if r.recommendations:
        w("## Recommendations")
        w("| ID | Priority | Category | Action | Approval |")
        w("|---|---|---|---|---|")
        for rec in r.recommendations:
            w(f"| {rec.id} | {rec.priority} | {rec.category} | {rec.action} | "
              f"{'required' if rec.requires_approval else '—'} |")
        w("")

    if r.risk is not None:  # failed runs were never analysed; do not imply a clean result
        active = [a for a in r.anomalies if a.counts_toward_risk]
        inactive = [a for a in r.anomalies if not a.counts_toward_risk]
        w(f"## Findings ({len(active)} active, {len(inactive)} suppressed/dismissed)")
        if not r.anomalies:
            w("No anomalies detected.")
        for a in active:
            override = f" (overridden from {a.original_severity.value})" if a.original_severity else ""
            w(f"### {SEV_ICON[a.severity.value]} {a.title}")
            w(f"`{a.type}` · severity **{a.severity.value}**{override} · score {a.score:.2f} · {a.status.value} · "
              f"fp `{a.fingerprint}`")
            w("")
            w(a.description)
            if a.transaction_ids:
                w(f"<sub>transactions: {', '.join(a.transaction_ids)}</sub>")
            if a.reviewer_note:
                w(f"<sub>reviewer: {a.reviewer_note}</sub>")
            w("")
        for a in inactive:
            label = "suppressed (previously dismissed)" if a.status == FindingStatus.SUPPRESSED else "dismissed"
            w(f"- ~~{a.title}~~ — {label}: {a.reviewer_note or ''}")
        w("")


    if r.portfolio:
        p = r.portfolio
        w("## Portfolio")
        w(f"Total {_usd(p.total_market_value)} · unrealised P&L {_usd(p.unrealized_pnl)} · "
          f"est. volatility {_pct(p.weighted_volatility)}")
        w("")
        w("| Asset class | Actual | Target |")
        w("|---|---|---|")
        for k, v in p.allocation.items():
            w(f"| {k} | {_pct(v)} | {_pct(p.target_allocation.get(k)) if p.target_allocation else 'n/a'} |")
        w("")
        w("Top positions: " + ", ".join(f"{t} {_pct(x)}" for t, x in list(p.position_weights.items())[:5]))
        w("")

    if r.cashflow:
        c = r.cashflow
        w("## Cash flow")
        w(f"Avg income {_usd(c.avg_monthly_income)}/mo · avg spend {_usd(c.avg_monthly_spend)}/mo · "
          f"savings rate {_pct(c.savings_rate)} · liquid assets {_usd(c.liquid_assets)} · "
          f"buffer {c.cash_buffer_months if c.cash_buffer_months is not None else 'n/a'} months")
        w("")

    if r.review:
        w("## Human review")
        if r.review.get("pending"):
            w("**Pending** — " + "; ".join(r.review.get("reasons", [])))
        elif not r.review.get("required"):
            w("Not required (no findings above review threshold).")
        else:
            d = r.review.get("decision", {})
            w(f"Reviewer **{d.get('reviewer')}** — {'approved' if d.get('approved') else 'REJECTED'} "
              f"at {d.get('decided_at')}. {d.get('notes') or ''}")
            for item in r.review.get("applied", []):
                w(f"- `{item['fingerprint']}` → {item['action']}")
        w("")

    if r.data_quality or r.analysis_notes:
        w("## Data quality & caveats")
        for n in r.analysis_notes:
            w(f"- {n}")
        for i in r.data_quality:
            w(f"- [{i.severity}] `{i.source}.{i.code}` {i.message}")
        w("")

    w("## Execution trace")
    w("| Step | Agent | Status | ms | Notes |")
    w("|---|---|---|---|---|")
    for s in r.execution_trace:
        notes = "; ".join(s.warnings + ([s.error] if s.error else []))
        w(f"| {s.step} | {s.agent} | {s.status.value} | {s.duration_ms:.1f} | {notes} |")
    return "\n".join(out) + "\n"
