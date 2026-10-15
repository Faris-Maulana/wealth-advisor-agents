"""REST API smoke test (skipped when the optional 'api' extra is not installed)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


def test_api_run_review_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WA_LOG_DIR", str(tmp_path / "logs"))
    import importlib

    from fastapi.testclient import TestClient

    import wealth_advisor.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    assert c.get("/health").json()["status"] == "ok"
    assert "C001" in c.get("/clients").json()
    assert c.post("/runs", json={"client_id": "../etc"}).status_code == 422

    run = c.post("/runs", json={"client_id": "C002"}).json()
    assert run["status"] == "awaiting_review"
    dup = next(a["fingerprint"] for a in run["anomalies"] if a["type"] == "duplicate_charge")
    done = c.post(f"/runs/{run['run_id']}/review", json={
        "approved": True, "reviewer": "qa",
        "finding_decisions": [{"fingerprint": dup, "action": "dismiss", "note": "ok"}]}).json()
    assert done["status"] == "completed"
    assert c.post(f"/runs/{run['run_id']}/review", json={"approved": True, "reviewer": "qa"}).status_code == 409
    assert c.get("/runs/nope").status_code == 404
    assert c.get(f"/runs/{run['run_id']}/events").json()
    degraded = c.post("/runs", json={"client_id": "C005", "crm_fault": "down"}).json()
    assert "crm" in degraded["degraded_sources"]
