"""Data Fetcher Agent: gathers and prepares everything downstream agents need.

Criticality policy
------------------
* client financial data  -> critical. No data, no analysis: fail the run.
* FX rates               -> degrade to USD-only (non-USD rows are dropped and reported).
* CRM profile            -> degrade to the cached snapshot from long-term memory
                            (if fresh enough), else continue without a profile
                            and let compliance checks report it as unverified.
* market quotes          -> degrade to cost-basis valuation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..errors import CriticalStepError
from ..models import ClientContext, ClientProfile, DataQualityIssue, StepStatus, from_dict, to_jsonable
from ..tools.market_data import FALLBACK_FX_TO_USD
from .base import Agent, AgentResult, RunContext, Task


class DataFetcherAgent(Agent):
    name = "data_fetcher"
    capabilities = frozenset({"fetch_client_context"})
    required_tools = frozenset({"client_data.load", "data_prep.normalize", "crm.get_client_profile",
                                "market_data.get_quotes", "market_data.get_fx_rates"})

    def execute(self, task: Task, ctx: RunContext) -> AgentResult:
        client_id = ctx.client_id
        warnings: list[str] = []
        degraded: list[str] = []

        raw = self.tools.call("client_data.load", client_id=client_id)
        if not raw.ok:
            raise CriticalStepError(f"cannot load financial data for '{client_id}': {raw.error}")

        fx = self.tools.call("market_data.get_fx_rates")
        fx_rates = fx.data if fx.ok else dict(FALLBACK_FX_TO_USD)
        if not fx.ok:
            degraded.append("fx_rates")
            warnings.append("FX rates unavailable; non-USD transactions excluded")
            self.decide("fx_fallback_usd_only", reason=fx.error)

        norm = self.tools.call("data_prep.normalize", record=raw.data, fx_to_usd=fx_rates)
        if not norm.ok:
            raise CriticalStepError(f"client data for '{client_id}' failed validation: {norm.error}")
        data = norm.data
        issues: list[DataQualityIssue] = list(data["issues"])

        profile = self._fetch_profile(ctx, issues, degraded, warnings)
        quotes = self._fetch_quotes(data["holdings"], issues, degraded, warnings)

        context = ClientContext(
            client_id=data["client_id"], name=data["name"], as_of=data["as_of"],
            base_currency=data["base_currency"], home_country=data["home_country"],
            accounts=data["accounts"], holdings=data["holdings"], transactions=data["transactions"],
            profile=profile, quotes=quotes, data_quality=issues, degraded_sources=degraded,
        )
        ctx.session.put(ctx.run_id, "context", to_jsonable(context))
        if issues:
            warnings.append(f"{len(issues)} data-quality issue(s) recorded")
        self.log.info("data.assembled", transactions=len(context.transactions), holdings=len(context.holdings),
                      quotes=len(quotes), profile_source=profile.source, data_issues=len(issues),
                      degraded_sources=degraded)
        status = StepStatus.DEGRADED if degraded or any(i.severity == "error" for i in issues) else StepStatus.OK
        return AgentResult(status, output=context, warnings=warnings)

    def _fetch_profile(self, ctx: RunContext, issues: list, degraded: list, warnings: list) -> ClientProfile:
        res = self.tools.call("crm.get_client_profile", client_id=ctx.client_id)
        if res.ok:
            ctx.long_term.save_crm_snapshot(ctx.client_id, to_jsonable(res.data))
            return res.data

        degraded.append("crm")
        if res.error_type == "ResourceNotFoundError":
            # The client is genuinely missing from the CRM: a data-integrity problem, not an outage.
            # Serving a cached profile here could mask an offboarded/merged record.
            self.decide("crm_profile_unavailable", reason="client not found in CRM", cache_used=False)
            issues.append(DataQualityIssue("crm", "client_not_in_crm",
                                           f"client {ctx.client_id} not found in CRM", severity="error"))
            warnings.append("client not found in CRM; compliance checks limited")
            return ClientProfile(client_id=ctx.client_id, source="unavailable")

        snap = ctx.long_term.get_crm_snapshot(ctx.client_id)
        if snap:
            payload, saved_at = snap
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(saved_at)).days
            if age_days <= ctx.settings.crm_cache_max_age_days:
                profile = from_dict(ClientProfile, payload)
                profile.source, profile.stale, profile.fetched_at = "cache", True, saved_at
                self.decide("crm_fallback_cache", reason=res.error, snapshot_age_days=age_days)
                issues.append(DataQualityIssue("crm", "stale_profile",
                                               f"CRM unavailable ({res.error_type}); using cached profile from {saved_at}"))
                warnings.append("CRM unavailable; using cached profile")
                return profile
            self.decide("crm_cache_too_old", snapshot_age_days=age_days,
                        max_age_days=ctx.settings.crm_cache_max_age_days)
        self.decide("crm_profile_unavailable", reason=res.error, cache_used=False)
        issues.append(DataQualityIssue("crm", "crm_unavailable",
                                       f"CRM unavailable ({res.error_type}: {res.error}) and no usable cache",
                                       severity="error"))
        warnings.append("CRM unavailable and no cached profile; compliance checks skipped")
        return ClientProfile(client_id=ctx.client_id, source="unavailable")

    def _fetch_quotes(self, holdings: list, issues: list, degraded: list, warnings: list) -> dict:
        if not holdings:
            return {}
        res = self.tools.call("market_data.get_quotes", tickers=[h.ticker for h in holdings])
        if not res.ok:
            degraded.append("market_data")
            warnings.append("market data unavailable; holdings valued at cost basis")
            self.decide("valuation_fallback_cost_basis", reason=res.error)
            issues.append(DataQualityIssue("market_data", "quotes_unavailable",
                                           f"quotes unavailable ({res.error_type}); valued at cost basis",
                                           severity="error"))
            return {}
        for t in res.data["unknown"]:
            issues.append(DataQualityIssue("market_data", "unknown_ticker",
                                           f"no quote for {t}; valued at cost basis, excluded from volatility", t))
        return res.data["quotes"]
