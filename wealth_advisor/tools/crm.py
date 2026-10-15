"""CRM integration.

`CRMBackend` is the transport (mock in-memory DB, or a real HTTP API);
`CRMProfileTool` owns the contract: it validates the payload and maps it to a
typed `ClientProfile`. Agents only ever see the tool.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Optional

from ..errors import (
    PermanentToolError,
    ResourceNotFoundError,
    SchemaValidationError,
    ToolTimeoutError,
    TransientToolError,
)
from ..models import ClientProfile
from .base import ParamSpec, Tool, ToolSpec
from .faults import FaultInjector

REQUIRED_FIELDS = ("client_id", "risk_profile", "kyc_status", "kyc_expiry")
VALID_RISK_PROFILES = {"conservative", "moderate", "aggressive"}


class CRMBackend(ABC):
    @abstractmethod
    def get_client(self, client_id: str) -> dict:
        ...


class MockCRMBackend(CRMBackend):
    """Simulated CRM backed by a dict (loaded from data/crm.json)."""

    def __init__(self, records: dict[str, dict], faults: Optional[FaultInjector] = None):
        self.records = records
        self.faults = faults or FaultInjector()

    def get_client(self, client_id: str) -> dict:
        self.faults.before_call("crm")
        if client_id not in self.records:
            raise ResourceNotFoundError(f"crm: HTTP 404 client '{client_id}' not found")
        payload = dict(self.records[client_id])
        if self.faults.corrupt:
            payload.pop("risk_profile", None)
            payload["kyc_expiry"] = "31/12/2026"
        return payload


class HTTPCRMBackend(CRMBackend):
    """Real REST transport: GET {base_url}/clients/{id}. Maps HTTP semantics to tool errors."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def get_client(self, client_id: str) -> dict:
        url = f"{self.base_url}/clients/{urllib.parse.quote(client_id, safe='')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ResourceNotFoundError(f"crm: HTTP 404 client '{client_id}' not found") from exc
            if exc.code == 429 or exc.code >= 500:
                raise TransientToolError(f"crm: HTTP {exc.code}") from exc
            raise PermanentToolError(f"crm: HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise ToolTimeoutError(f"crm: timed out after {self.timeout_s}s") from exc
        except urllib.error.URLError as exc:
            raise TransientToolError(f"crm: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise SchemaValidationError("crm: response is not valid JSON") from exc


def _parse_date(value, field_name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise SchemaValidationError(f"crm: field '{field_name}' is not ISO-8601: {value!r}") from exc


def parse_crm_payload(payload: dict) -> ClientProfile:
    missing = [f for f in REQUIRED_FIELDS if payload.get(f) in (None, "")]
    if missing:
        raise SchemaValidationError(f"crm: payload missing required field(s) {missing}")
    risk = str(payload["risk_profile"]).lower()
    if risk not in VALID_RISK_PROFILES:
        raise SchemaValidationError(f"crm: unknown risk_profile '{risk}'")
    return ClientProfile(
        client_id=payload["client_id"],
        risk_profile=risk,
        risk_profile_updated=_parse_date(payload.get("risk_profile_updated"), "risk_profile_updated"),
        kyc_status=str(payload["kyc_status"]).lower(),
        kyc_expiry=_parse_date(payload["kyc_expiry"], "kyc_expiry"),
        age=payload.get("age"),
        advisor=payload.get("advisor"),
        segment=payload.get("segment"),
        investment_horizon_years=payload.get("investment_horizon_years"),
        usual_countries=[str(c).upper() for c in payload.get("usual_countries", [])],
        goals=list(payload.get("goals", [])),
        last_contact=_parse_date(payload.get("last_contact"), "last_contact"),
        source="crm",
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


class CRMProfileTool(Tool):
    spec = ToolSpec(
        name="crm.get_client_profile",
        description="Fetch the client's CRM profile: risk profile, KYC status, advisor, goals.",
        params={"client_id": ParamSpec(str)},
    )

    def __init__(self, backend: CRMBackend):
        self.backend = backend

    def run(self, client_id: str) -> ClientProfile:
        return parse_crm_payload(self.backend.get_client(client_id))
