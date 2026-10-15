from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wealth_advisor.app import Faults, build_system
from wealth_advisor.config import Settings
from wealth_advisor.hitl import AutoApproveReviewer, DeferredReviewer
from wealth_advisor.logging_setup import configure_logging
from wealth_advisor.memory import InMemoryLongTermMemory
from wealth_advisor.models import Transaction


@pytest.fixture(autouse=True, scope="session")
def _quiet_logs():
    configure_logging("CRITICAL", console=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path / "state", log_dir=tmp_path / "logs")


@pytest.fixture
def make_system(settings):
    def _make(reviewer=None, crm="none", market="none", long_term=None, **kw):
        return build_system(settings, faults=Faults(crm=crm, market_data=market),
                            reviewer=reviewer or AutoApproveReviewer(),
                            long_term=long_term or InMemoryLongTermMemory(), sleep=lambda s: None, **kw)
    return _make


@pytest.fixture
def deferred():
    return DeferredReviewer()


T0 = datetime(2026, 6, 1, 9, 0)


def txn(i: int, amount: float, *, days: float = 0, category: str = "shopping", counterparty: str = "Shop",
        channel: str = "card", country: str = "US") -> Transaction:
    return Transaction(txn_id=f"T{i:04d}", timestamp=T0 + timedelta(days=days), amount=amount, category=category,
                       counterparty=counterparty, channel=channel, country=country)
