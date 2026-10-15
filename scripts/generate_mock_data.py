"""Generate deterministic mock data for the Wealth Advisor Assistant.

Produces:
  data/clients/<id>.json   client financial records (accounts, holdings, transactions)
  data/crm.json            mock CRM database
  data/market_data.json    mock quotes, sector/asset-class metadata, FX rates

Each client is engineered to exercise a different part of the system:
  C001  moderate investor; concentration risk + rising discretionary spend
  C002  conservative retiree; AML-style red flags (structuring, overseas wire,
        duplicate charge) + expired KYC
  C003  young aggressive investor; income stop, P2P velocity burst, low cash
        buffer, stated-vs-actual risk profile mismatch
  C004  data-quality edge cases; missing/invalid fields, multi-currency,
        duplicate record ids, unknown ticker, absent from CRM
  C005  clean baseline; should produce no material findings

Run:  python scripts/generate_mock_data.py
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
START = datetime(2026, 4, 1)
END = datetime(2026, 9, 30, 23, 59)
AS_OF = "2026-09-30"


class TxnFactory:
    def __init__(self, client_id: str, seed: int):
        self.client_id = client_id
        self.rng = random.Random(seed)
        self.n = 0
        self.items: list[dict] = []

    def add(self, ts: datetime, amount: float, category: str, counterparty: str,
            channel: str = "card", country: str = "US", account_id: str = "CHK",
            currency: str = "USD", **extra) -> dict:
        self.n += 1
        txn = {
            "txn_id": f"{self.client_id}-T{self.n:04d}",
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "amount": round(amount, 2) if isinstance(amount, (int, float)) else amount,
            "currency": currency,
            "category": category,
            "counterparty": counterparty,
            "channel": channel,
            "country": country,
            "account_id": f"{self.client_id}-{account_id}",
        }
        txn.update(extra)
        self.items.append(txn)
        return txn

    def days(self):
        d = START
        while d <= END:
            yield d
            d += timedelta(days=1)

    def at(self, day: datetime, hour_lo: int = 8, hour_hi: int = 21) -> datetime:
        return day.replace(hour=self.rng.randint(hour_lo, hour_hi),
                           minute=self.rng.randint(0, 59), second=self.rng.randint(0, 59))

    def routine_spend(self, monthly_scale=lambda m: 1.0, rent: float = 0.0,
                      rent_payee: str = "Landlord", grocery=(60, 180), dining=(25, 90),
                      dining_p=0.25, grocery_p=0.18):
        for day in self.days():
            scale = monthly_scale(day.month)
            if day.day == 1 and rent:
                self.add(self.at(day, 9, 10), -rent, "housing", rent_payee, channel="ach")
            if day.day == 5:
                self.add(self.at(day), -self.rng.uniform(140, 190), "utilities", "City Power & Water", channel="ach")
                self.add(self.at(day), -89.99, "utilities", "FiberNet Internet", channel="ach")
            if self.rng.random() < grocery_p:
                self.add(self.at(day), -self.rng.uniform(*grocery) * scale, "groceries",
                         self.rng.choice(["FreshMart", "Green Grocer", "Costco"]))
            if self.rng.random() < dining_p * scale:
                self.add(self.at(day, 11, 22), -self.rng.uniform(*dining) * scale, "dining",
                         self.rng.choice(["Blue Bottle", "Sushi Den", "Pasta Bar", "Taqueria Sol", "Thai Orchid"]))
            if self.rng.random() < 0.08:
                self.add(self.at(day), -self.rng.uniform(20, 70), "transport",
                         self.rng.choice(["Shell", "Uber", "Metro Transit"]))
            if self.rng.random() < 0.06 * scale:
                self.add(self.at(day), -self.rng.uniform(40, 260) * scale, "shopping",
                         self.rng.choice(["Amazon", "Target", "Uniqlo", "Best Buy"]))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def client_c001() -> dict:
    f = TxnFactory("C001", 101)
    # Discretionary spend ramps from Jul onwards (+~40%).
    scale = lambda m: 1.0 if m <= 6 else 1.0 + 0.2 * (m - 6)  # noqa: E731
    f.routine_spend(monthly_scale=scale, rent=3200, rent_payee="Harbor View Apartments")
    for day in f.days():
        if day.weekday() == 4 and (day - START).days % 14 == 3:
            f.add(day.replace(hour=6), 6800.00, "salary", "Northwind Technologies", channel="ach")
        if day.day == 15:
            f.add(f.at(day), -1500.00, "investment_transfer", "Own Brokerage", channel="internal")
    f.add(datetime(2026, 8, 12, 14, 5), -4380.00, "travel", "Lumen Airlines")
    f.add(datetime(2026, 8, 20, 10, 30), -2950.00, "travel", "Kyoto Ryokan Stay", country="JP")
    f.add(datetime(2026, 9, 3, 11, 0), -18500.00, "education", "Westbrook Academy", channel="wire")
    return {
        "client_id": "C001",
        "name": "Amelia Chen",
        "as_of": AS_OF,
        "base_currency": "USD",
        "home_country": "US",
        "accounts": [
            {"account_id": "C001-CHK", "type": "checking", "balance": 21450.00, "currency": "USD"},
            {"account_id": "C001-SAV", "type": "savings", "balance": 78000.00, "currency": "USD"},
            {"account_id": "C001-BRK", "type": "brokerage", "balance": 4200.00, "currency": "USD"},
        ],
        "holdings": [
            {"ticker": "NVDA", "quantity": 900, "cost_basis": 61000},
            {"ticker": "AAPL", "quantity": 420, "cost_basis": 64000},
            {"ticker": "VTI", "quantity": 310, "cost_basis": 72000},
            {"ticker": "BND", "quantity": 820, "cost_basis": 60000},
            {"ticker": "VXUS", "quantity": 400, "cost_basis": 23000},
        ],
        "transactions": f.items,
    }


def client_c002() -> dict:
    f = TxnFactory("C002", 202)
    f.routine_spend(grocery=(40, 120), dining=(20, 60), dining_p=0.12, grocery_p=0.2)
    for day in f.days():
        if day.day == 1:
            f.add(day.replace(hour=5), 4200.00, "pension", "State Teachers Pension Fund", channel="ach")
        if day.day == 3:
            f.add(day.replace(hour=5), 2900.00, "social_security", "SSA Treasury", channel="ach")
        if day.day == 10:
            f.add(f.at(day), -420.00, "healthcare", "Medicare Supplement Plan", channel="ach")
        if day.day in (8, 22) and f.rng.random() < 0.8:
            f.add(f.at(day), -f.rng.uniform(30, 90), "healthcare", "Riverside Pharmacy")
    # Red flags (September).
    for i, (d, amt) in enumerate([(9, 9500), (11, 9800), (12, 9400), (14, 9900)]):
        f.add(datetime(2026, 9, d, 10 + i, 15), amt, "cash_deposit", "Branch Deposit - Main St", channel="cash")
    f.add(datetime(2026, 9, 16, 15, 42), -48000.00, "transfer_out", "Meridian Holdings Ltd",
          channel="wire", country="CY", memo="Investment opportunity")
    f.add(datetime(2026, 9, 18, 17, 2, 11), -286.40, "healthcare", "Riverside Pharmacy")
    f.add(datetime(2026, 9, 18, 17, 4, 49), -286.40, "healthcare", "Riverside Pharmacy")
    return {
        "client_id": "C002",
        "name": "Robert Lindqvist",
        "as_of": AS_OF,
        "base_currency": "USD",
        "home_country": "US",
        "accounts": [
            {"account_id": "C002-CHK", "type": "checking", "balance": 31200.00, "currency": "USD"},
            {"account_id": "C002-SAV", "type": "savings", "balance": 85000.00, "currency": "USD"},
            {"account_id": "C002-BRK", "type": "brokerage", "balance": 12000.00, "currency": "USD"},
        ],
        "holdings": [
            {"ticker": "BND", "quantity": 3200, "cost_basis": 236000},
            {"ticker": "VTIP", "quantity": 1500, "cost_basis": 73000},
            {"ticker": "SCHD", "quantity": 1100, "cost_basis": 82000},
            {"ticker": "JNJ", "quantity": 250, "cost_basis": 39000},
            {"ticker": "PG", "quantity": 200, "cost_basis": 31000},
        ],
        "transactions": f.items,
    }


def client_c003() -> dict:
    f = TxnFactory("C003", 303)
    f.routine_spend(rent=2400, rent_payee="Mission Lofts", dining_p=0.35)
    for day in f.days():
        # Salary stops after July (job change).
        if day.month <= 7 and day.day in (1, 15):
            f.add(day.replace(hour=6), 4600.00, "salary", "Brightpath Media", channel="ach")
        if day.day == 20:
            f.add(f.at(day), -650.00, "investment_transfer", "Own Brokerage", channel="internal")
    f.add(datetime(2026, 9, 6, 9, 10), 1200.00, "freelance", "Upwork Escrow", channel="ach")
    # P2P velocity burst.
    for i, (who, amt) in enumerate([("J. Alvarez", 900), ("K. Osei", 1250), ("M. Ruiz", 700),
                                     ("T. Nguyen", 1500), ("S. Patel", 1100)]):
        f.add(datetime(2026, 9, 21, 9, 0) + timedelta(hours=7 * i), -amt, "p2p_transfer", who, channel="p2p")
    return {
        "client_id": "C003",
        "name": "Priya Nair",
        "as_of": AS_OF,
        "base_currency": "USD",
        "home_country": "US",
        "accounts": [
            {"account_id": "C003-CHK", "type": "checking", "balance": 3150.00, "currency": "USD"},
            {"account_id": "C003-SAV", "type": "savings", "balance": 1900.00, "currency": "USD"},
            {"account_id": "C003-BRK", "type": "brokerage", "balance": 350.00, "currency": "USD"},
        ],
        "holdings": [
            {"ticker": "TSLA", "quantity": 160, "cost_basis": 38000},
            {"ticker": "NVDA", "quantity": 110, "cost_basis": 11000},
            {"ticker": "ARKK", "quantity": 300, "cost_basis": 16000},
            {"ticker": "IBIT", "quantity": 240, "cost_basis": 9500},
        ],
        "transactions": f.items,
    }


def client_c004() -> dict:
    f = TxnFactory("C004", 404)
    for m in range(4, 10):
        f.add(datetime(2026, m, 1, 6), 7200.00, "salary", "Atlas Logistics", channel="ach")
        f.add(datetime(2026, m, 2, 9), -2800.00, "housing", "Elm Street Rentals", channel="ach")
        f.add(datetime(2026, m, 9, 13), -f.rng.uniform(150, 320), "groceries", "FreshMart")
    f.add(datetime(2026, 6, 14, 12), -640.00, "travel", "Hotel Adlon", currency="EUR", country="DE")
    f.add(datetime(2026, 7, 3, 19), -210.00, "dining", "Marina Bay Grill", currency="SGD", country="SG")
    f.add(datetime(2026, 7, 4, 10), -99.00, "shopping", "Unknown FX Merchant", currency="XYZ", country="ZZ")
    bad_amount = f.add(datetime(2026, 8, 1, 10), 0, "shopping", "Amazon")
    del bad_amount["amount"]  # missing amount
    f.add(datetime(2026, 8, 2, 10), -55.00, "dining", "Pasta Bar")["timestamp"] = "2026-13-45T99:00:00"
    f.add(datetime(2026, 8, 5, 10), "not-a-number", "shopping", "Target")
    dup = dict(f.items[3])  # duplicate record id (ingestion replay)
    f.items.append(dup)
    return {
        "client_id": "C004",
        "name": "Marcus Oyelaran",
        "as_of": AS_OF,
        "base_currency": "USD",
        "home_country": "US",
        "accounts": [
            {"account_id": "C004-CHK", "type": "checking", "balance": 12800.00, "currency": "USD"},
            {"account_id": "C004-SAV", "type": "savings", "balance": None, "currency": "USD"},
        ],
        "holdings": [
            {"ticker": "VTI", "quantity": 150, "cost_basis": 36000},
            {"ticker": "PRIVCO", "quantity": 1000, "cost_basis": 25000},
            {"ticker": "BND", "quantity": -10, "cost_basis": 700},
        ],
        "transactions": f.items,
    }


def client_c005() -> dict:
    f = TxnFactory("C005", 505)
    f.routine_spend(rent=2100, rent_payee="Oak Park Residences", dining_p=0.2)
    for day in f.days():
        if day.day in (1, 15):
            f.add(day.replace(hour=6), 5200.00, "salary", "Cedar Health Systems", channel="ach")
        if day.day == 16:
            f.add(f.at(day), -1200.00, "investment_transfer", "Own Brokerage", channel="internal")
    return {
        "client_id": "C005",
        "name": "Sofia Marquez",
        "as_of": AS_OF,
        "base_currency": "USD",
        "home_country": "US",
        "accounts": [
            {"account_id": "C005-CHK", "type": "checking", "balance": 14800.00, "currency": "USD"},
            {"account_id": "C005-SAV", "type": "savings", "balance": 32000.00, "currency": "USD"},
            {"account_id": "C005-BRK", "type": "brokerage", "balance": 9000.00, "currency": "USD"},
        ],
        "holdings": [
            {"ticker": "VTI", "quantity": 330, "cost_basis": 76000},
            {"ticker": "VXUS", "quantity": 900, "cost_basis": 52000},
            {"ticker": "BND", "quantity": 1050, "cost_basis": 76000},
            {"ticker": "GLD", "quantity": 40, "cost_basis": 8200},
        ],
        "transactions": f.items,
    }


CRM = {
    "C001": {"client_id": "C001", "advisor": "J. Whitfield", "segment": "mass_affluent",
             "risk_profile": "moderate", "risk_profile_updated": "2026-01-20",
             "kyc_status": "verified", "kyc_expiry": "2027-05-01", "age": 42,
             "investment_horizon_years": 15, "usual_countries": ["US", "JP"],
             "last_contact": "2026-07-02", "goals": ["children education", "retirement at 60"]},
    "C002": {"client_id": "C002", "advisor": "J. Whitfield", "segment": "retiree",
             "risk_profile": "conservative", "risk_profile_updated": "2025-03-11",
             "kyc_status": "verified", "kyc_expiry": "2026-08-15", "age": 71,
             "investment_horizon_years": 8, "usual_countries": ["US"],
             "last_contact": "2026-02-18", "goals": ["capital preservation", "income"]},
    "C003": {"client_id": "C003", "advisor": "L. Romero", "segment": "emerging",
             "risk_profile": "moderate", "risk_profile_updated": "2026-02-02",
             "kyc_status": "verified", "kyc_expiry": "2027-02-02", "age": 29,
             "investment_horizon_years": 30, "usual_countries": ["US"],
             "last_contact": "2026-05-30", "goals": ["home purchase in 5y"]},
    # C004 intentionally absent (CRM returns 404).
    "C005": {"client_id": "C005", "advisor": "L. Romero", "segment": "mass_affluent",
             "risk_profile": "moderate", "risk_profile_updated": "2026-04-10",
             "kyc_status": "verified", "kyc_expiry": "2027-10-10", "age": 37,
             "investment_horizon_years": 25, "usual_countries": ["US"],
             "last_contact": "2026-08-21", "goals": ["retirement"]},
}

MARKET = {
    "as_of": AS_OF,
    "quotes": {
        "AAPL": {"price": 238.40, "asset_class": "equity", "sector": "technology", "volatility": 0.27},
        "NVDA": {"price": 182.10, "asset_class": "equity", "sector": "technology", "volatility": 0.52},
        "TSLA": {"price": 251.30, "asset_class": "equity", "sector": "consumer_discretionary", "volatility": 0.61},
        "ARKK": {"price": 61.20, "asset_class": "equity", "sector": "technology", "volatility": 0.55},
        "IBIT": {"price": 58.90, "asset_class": "alternative", "sector": "digital_assets", "volatility": 0.68},
        "JNJ": {"price": 162.75, "asset_class": "equity", "sector": "healthcare", "volatility": 0.16},
        "PG": {"price": 168.30, "asset_class": "equity", "sector": "consumer_staples", "volatility": 0.15},
        "SCHD": {"price": 82.40, "asset_class": "equity", "sector": "diversified", "volatility": 0.14},
        "VTI": {"price": 305.60, "asset_class": "equity", "sector": "diversified", "volatility": 0.17},
        "VXUS": {"price": 66.10, "asset_class": "equity", "sector": "diversified", "volatility": 0.16},
        "BND": {"price": 74.20, "asset_class": "fixed_income", "sector": "bonds", "volatility": 0.06},
        "VTIP": {"price": 49.80, "asset_class": "fixed_income", "sector": "bonds", "volatility": 0.03},
        "GLD": {"price": 241.50, "asset_class": "alternative", "sector": "commodities", "volatility": 0.15},
    },
    "fx_to_usd": {"USD": 1.0, "EUR": 1.08, "SGD": 0.74, "GBP": 1.27, "IDR": 0.0000615, "JPY": 0.0068},
}


def main() -> None:
    for build in (client_c001, client_c002, client_c003, client_c004, client_c005):
        record = build()
        write_json(DATA / "clients" / f"{record['client_id']}.json", record)
        print(f"wrote {record['client_id']}: {len(record['transactions'])} transactions")
    write_json(DATA / "crm.json", CRM)
    write_json(DATA / "market_data.json", MARKET)
    print("wrote crm.json, market_data.json")


if __name__ == "__main__":
    main()
