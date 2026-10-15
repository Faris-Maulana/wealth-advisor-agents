"""Market data: quotes (price, asset class, sector, volatility) and FX rates."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..errors import SchemaValidationError
from ..models import Quote
from .base import ParamSpec, Tool, ToolSpec
from .faults import FaultInjector

# Static fallback so a market-data outage degrades valuation rather than
# blocking currency normalisation entirely.
FALLBACK_FX_TO_USD = {"USD": 1.0}


class MarketDataBackend(ABC):
    @abstractmethod
    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        ...

    @abstractmethod
    def fx_to_usd(self) -> dict[str, float]:
        ...


class MockMarketDataBackend(MarketDataBackend):
    def __init__(self, dataset: dict, faults: Optional[FaultInjector] = None):
        self.dataset = dataset
        self.faults = faults or FaultInjector()

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        self.faults.before_call("market_data")
        known = self.dataset.get("quotes", {})
        out = {t: dict(known[t]) for t in tickers if t in known}
        if self.faults.corrupt:
            for q in out.values():
                q["price"] = None
        return out

    def fx_to_usd(self) -> dict[str, float]:
        self.faults.before_call("market_data")
        return dict(self.dataset.get("fx_to_usd", {}))


class QuotesTool(Tool):
    spec = ToolSpec(
        name="market_data.get_quotes",
        description="Latest price, asset class, sector and annualised volatility for tickers.",
        params={"tickers": ParamSpec(list)},
    )

    def __init__(self, backend: MarketDataBackend):
        self.backend = backend

    def run(self, tickers: list) -> dict:
        tickers = sorted({str(t).upper() for t in tickers})
        raw = self.backend.quotes(tickers)
        quotes: dict[str, Quote] = {}
        for t, q in raw.items():
            price = q.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                raise SchemaValidationError(f"market_data: invalid price for {t}: {price!r}")
            quotes[t] = Quote(t, float(price), q.get("asset_class", "equity"), q.get("sector", "unknown"),
                              float(q.get("volatility") or 0.0))
        return {"quotes": quotes, "unknown": [t for t in tickers if t not in quotes]}


class FxRatesTool(Tool):
    spec = ToolSpec(
        name="market_data.get_fx_rates",
        description="FX conversion rates to USD.",
        params={},
    )

    def __init__(self, backend: MarketDataBackend):
        self.backend = backend

    def run(self) -> dict:
        rates = self.backend.fx_to_usd()
        if rates.get("USD") != 1.0:
            raise SchemaValidationError("market_data: FX table must contain USD=1.0")
        return rates
