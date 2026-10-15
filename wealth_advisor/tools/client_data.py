"""Client financial data: source abstraction, loading and normalisation.

Loading (I/O) and normalisation (pure transformation) are separate tools so the
normaliser can be unit-tested in isolation and reused for other sources.

Normalisation policy: be strict about the record envelope (no client id or no
transaction list => unusable, fail the run) but lenient about individual rows
(drop and report). A single malformed transaction should never block advice.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..errors import PermanentToolError, ResourceNotFoundError, SchemaValidationError
from ..models import Account, DataQualityIssue, Holding, Transaction
from .base import ParamSpec, Tool, ToolSpec


class ClientDataSource(ABC):
    @abstractmethod
    def fetch(self, client_id: str) -> dict:
        ...

    @abstractmethod
    def list_ids(self) -> list[str]:
        ...


class FileClientDataSource(ClientDataSource):
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def fetch(self, client_id: str) -> dict:
        if not client_id.replace("-", "").replace("_", "").isalnum():
            raise PermanentToolError(f"invalid client id '{client_id}'")  # blocks path traversal
        path = self.directory / f"{client_id}.json"
        if not path.exists():
            raise ResourceNotFoundError(f"no financial data file for client '{client_id}'")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(f"client file '{path.name}' is not valid JSON: {exc}") from exc

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))


class InMemoryClientDataSource(ClientDataSource):
    def __init__(self, records: dict[str, dict]):
        self.records = records

    def fetch(self, client_id: str) -> dict:
        if client_id not in self.records:
            raise ResourceNotFoundError(f"no financial data for client '{client_id}'")
        return json.loads(json.dumps(self.records[client_id]))  # defensive copy

    def list_ids(self) -> list[str]:
        return sorted(self.records)


class LoadClientDataTool(Tool):
    spec = ToolSpec(
        name="client_data.load",
        description="Load the raw financial record (accounts, holdings, transactions) for a client.",
        params={"client_id": ParamSpec(str, description="Client identifier, e.g. C001")},
    )

    def __init__(self, source: ClientDataSource):
        self.source = source

    def run(self, client_id: str) -> dict:
        record = self.source.fetch(client_id)
        if not isinstance(record, dict):
            raise SchemaValidationError("client record must be a JSON object")
        if record.get("client_id") != client_id:
            raise SchemaValidationError(
                f"client_id mismatch: requested '{client_id}', record has '{record.get('client_id')}'")
        if not isinstance(record.get("transactions"), list):
            raise SchemaValidationError("client record has no 'transactions' list")
        return record


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def normalize_client_record(record: dict, fx_to_usd: dict[str, float]) -> dict:
    """Validate and normalise a raw record. Returns typed objects plus data-quality issues."""
    issues: list[DataQualityIssue] = []
    base = record.get("base_currency", "USD")
    if base != "USD":
        # All analytics run in USD; non-USD base would need a cross-rate table.
        raise SchemaValidationError(f"unsupported base currency '{base}' (only USD supported)")

    try:
        as_of = date.fromisoformat(record.get("as_of", ""))
    except ValueError:
        as_of = date.today()
        issues.append(DataQualityIssue("client_data", "missing_as_of",
                                       f"as_of missing/invalid; defaulted to {as_of.isoformat()}"))

    accounts: list[Account] = []
    for raw in record.get("accounts", []) or []:
        bal = _num(raw.get("balance"))
        if bal is None:
            issues.append(DataQualityIssue("client_data", "unknown_balance",
                                           f"account {raw.get('account_id')} has no usable balance; treated as 0 "
                                           "(liquidity may be understated)", raw.get("account_id")))
            bal = 0.0
        accounts.append(Account(str(raw.get("account_id", "?")), str(raw.get("type", "unknown")), bal,
                                raw.get("currency", "USD")))

    holdings: list[Holding] = []
    for raw in record.get("holdings", []) or []:
        qty, cost = _num(raw.get("quantity")), _num(raw.get("cost_basis"))
        ticker = str(raw.get("ticker") or "").upper()
        if not ticker or qty is None or qty <= 0:
            issues.append(DataQualityIssue("client_data", "invalid_holding",
                                           f"holding {ticker or '?'} has invalid quantity {raw.get('quantity')!r}; excluded",
                                           ticker or None))
            continue
        holdings.append(Holding(ticker, qty, cost if cost is not None else 0.0))

    txns: list[Transaction] = []
    seen: set[str] = set()
    for idx, raw in enumerate(record["transactions"]):
        tid = raw.get("txn_id") or f"row-{idx}"
        if tid in seen:
            issues.append(DataQualityIssue("client_data", "duplicate_record",
                                           f"duplicate transaction id {tid}; kept first occurrence", tid))
            continue
        seen.add(tid)
        amount = _num(raw.get("amount"))
        if amount is None:
            issues.append(DataQualityIssue("client_data", "invalid_amount",
                                           f"transaction {tid} has missing/non-numeric amount {raw.get('amount')!r}; dropped",
                                           tid))
            continue
        try:
            ts = datetime.fromisoformat(str(raw.get("timestamp")))
        except ValueError:
            issues.append(DataQualityIssue("client_data", "invalid_timestamp",
                                           f"transaction {tid} has invalid timestamp {raw.get('timestamp')!r}; dropped",
                                           tid))
            continue
        if ts.date() > as_of:
            issues.append(DataQualityIssue("client_data", "future_dated",
                                           f"transaction {tid} is dated after as_of; dropped", tid))
            continue
        ccy = str(raw.get("currency") or "USD").upper()
        rate = fx_to_usd.get(ccy)
        if rate is None:
            issues.append(DataQualityIssue("client_data", "unknown_currency",
                                           f"transaction {tid} in unsupported currency {ccy}; dropped", tid))
            continue
        txns.append(Transaction(
            txn_id=tid, timestamp=ts, amount=round(amount * rate, 2),
            category=str(raw.get("category") or "uncategorized").lower(),
            counterparty=str(raw.get("counterparty") or "unknown").strip(),
            channel=str(raw.get("channel") or "card").lower(),
            country=str(raw.get("country") or record.get("home_country", "US")).upper(),
            account_id=str(raw.get("account_id") or ""),
            original_amount=amount, original_currency=ccy,
        ))
    txns.sort(key=lambda t: t.timestamp)
    if not txns:
        issues.append(DataQualityIssue("client_data", "no_transactions",
                                       "no valid transactions; cash-flow analysis will be limited", severity="error"))

    return {
        "client_id": record["client_id"],
        "name": record.get("name") or record["client_id"],
        "as_of": as_of,
        "home_country": str(record.get("home_country", "US")).upper(),
        "base_currency": base,
        "accounts": accounts,
        "holdings": holdings,
        "transactions": txns,
        "issues": issues,
    }


class NormalizeClientRecordTool(Tool):
    spec = ToolSpec(
        name="data_prep.normalize",
        description="Validate a raw client record, convert to base currency and drop malformed rows.",
        params={"record": ParamSpec(dict), "fx_to_usd": ParamSpec(dict)},
    )

    def run(self, record: dict, fx_to_usd: dict) -> dict:
        return normalize_client_record(record, fx_to_usd)


__all__ = ["ClientDataSource", "FileClientDataSource", "InMemoryClientDataSource", "LoadClientDataTool",
           "NormalizeClientRecordTool", "normalize_client_record"]
