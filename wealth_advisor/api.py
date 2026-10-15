"""Optional REST API (pip install -e ".[api]"; uvicorn wealth_advisor.api:app).

The API uses the deferred reviewer: a run that needs human validation returns
`awaiting_review`, and a reviewer (UI, ticketing system, another service)
completes it with POST /runs/{run_id}/review. This is the asynchronous form of
the HITL checkpoint.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .app import Faults, build_system
from .config import Settings
from .errors import InvalidRunStateError, RunNotFoundError
from .hitl import DeferredReviewer, load_decision
from .logging_setup import configure_logging, trace_recorder
from .memory import FileSessionMemory

FaultMode = Literal["none", "transient", "down", "slow", "corrupt"]

settings = Settings.from_env()
configure_logging("INFO", log_file=settings.log_dir / "api.jsonl")
_session = FileSessionMemory(settings.state_dir / "runs")
_memory_path = settings.state_dir / "memory.db"
_base = build_system(settings, reviewer=DeferredReviewer(), session=_session, memory_path=_memory_path)

app = FastAPI(title="Wealth Advisor Assistant", version="1.0.0")


class RunRequest(BaseModel):
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    crm_fault: FaultMode = "none"
    market_fault: FaultMode = "none"


class FindingDecisionIn(BaseModel):
    fingerprint: str
    action: Literal["confirm", "dismiss", "override"]
    severity: Optional[Literal["low", "medium", "high", "critical"]] = None
    note: Optional[str] = None


class ReviewIn(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1, max_length=64)
    notes: str = ""
    finding_decisions: list[FindingDecisionIn] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "tools": _base.registry.names(),
            "breakers": {n: _base.registry.breaker_state(n) for n in _base.registry.names()
                         if _base.registry.breaker_state(n)}}


@app.get("/clients")
def clients() -> list[str]:
    return _base.backends.client_data.list_ids()


@app.post("/runs")
def create_run(req: RunRequest) -> dict:
    system = _base
    if req.crm_fault != "none" or req.market_fault != "none":
        # Fault injection builds an isolated system sharing the same memory stores.
        system = build_system(settings, faults=Faults(req.crm_fault, req.market_fault), reviewer=DeferredReviewer(),
                              session=_session, long_term=_base.long_term)
    return system.orchestrator.run(req.client_id).to_dict()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    try:
        return {"status": _session.get(run_id, "status"), "report": _session.get(run_id, "report"),
                "review_request": _session.get(run_id, "review_request")}
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/runs/{run_id}/events")
def run_events(run_id: str) -> list[dict]:
    return trace_recorder().events(run_id)


@app.post("/runs/{run_id}/review")
def review(run_id: str, body: ReviewIn) -> dict:
    try:
        decision = load_decision(body.model_dump())
        return _base.orchestrator.resume(run_id, decision).to_dict()
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except InvalidRunStateError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/clients/{client_id}/history")
def history(client_id: str) -> dict:
    return {"runs": _base.long_term.history(client_id),
            "dismissed": _base.long_term.dismissed_fingerprints(client_id)}
