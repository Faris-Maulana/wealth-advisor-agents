"""Browser bridge: exposes the real `wealth_advisor` package to the static web demo (Pyodide).

The same orchestrator, agents, tools and memory classes run in the browser;
only the composition differs (in-memory stores, no-op sleep, deferred review so
the page can render the human-review checkpoint).
"""

from __future__ import annotations

import json
from pathlib import Path

from wealth_advisor.app import Faults, build_system
from wealth_advisor.config import Settings
from wealth_advisor.hitl import AutoApproveReviewer, DeferredReviewer, load_decision
from wealth_advisor.logging_setup import configure_logging, trace_recorder
from wealth_advisor.memory import InMemoryLongTermMemory, SessionMemory

APP_ROOT = Path(__file__).resolve().parent
SETTINGS = Settings(data_dir=APP_ROOT / "data", state_dir=APP_ROOT / ".state", log_dir=APP_ROOT / "logs")

configure_logging("DEBUG", console=False)
_session = SessionMemory()
_long_term = InMemoryLongTermMemory()


def _system(crm: str = "none", market: str = "none", auto: bool = False):
    return build_system(SETTINGS, faults=Faults(crm, market),
                        reviewer=AutoApproveReviewer("auto-policy (demo)") if auto else DeferredReviewer(),
                        session=_session, long_term=_long_term, sleep=lambda s: None)


def _payload(report) -> str:
    return json.dumps({"report": report.to_dict(), "events": trace_recorder().events(report.run_id)}, default=str)


def list_clients() -> str:
    out = []
    for cid in _system().backends.client_data.list_ids():
        rec = json.loads((SETTINGS.data_dir / "clients" / f"{cid}.json").read_text())
        out.append({"id": cid, "name": rec.get("name", cid), "transactions": len(rec.get("transactions", [])),
                    "holdings": len(rec.get("holdings", []))})
    out.append({"id": "C999", "name": "Unknown client (missing data)", "transactions": 0, "holdings": 0})
    return json.dumps(out)


def client_input(client_id: str) -> str:
    path = SETTINGS.data_dir / "clients" / f"{client_id}.json"
    crm = json.loads((SETTINGS.data_dir / "crm.json").read_text()).get(client_id)
    raw = json.loads(path.read_text()) if path.exists() else None
    return json.dumps({"client": raw, "crm": crm})


def run(client_id: str, crm: str, market: str, auto: bool) -> str:
    return _payload(_system(crm, market, auto).orchestrator.run(client_id))


def resume(run_id: str, decision_json: str) -> str:
    decision = load_decision(json.loads(decision_json))
    return _payload(_system().orchestrator.resume(run_id, decision))


def memory(client_id: str) -> str:
    return json.dumps({"runs": _long_term.history(client_id, 20),
                       "dismissed": _long_term.dismissed_fingerprints(client_id),
                       "crm_cached": _long_term.get_crm_snapshot(client_id) is not None}, default=str)


def reset_memory() -> str:
    global _long_term, _session
    _long_term, _session = InMemoryLongTermMemory(), SessionMemory()
    return "ok"
