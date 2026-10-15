"""Normalisation, memory stores, narrative guardrail, CLI, HTTP CRM backend."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wealth_advisor import cli
from wealth_advisor.errors import ResourceNotFoundError, SchemaValidationError, TransientToolError
from wealth_advisor.hitl import CLIReviewer
from wealth_advisor.memory import InMemoryLongTermMemory, SQLiteLongTermMemory
from wealth_advisor.models import ReviewRequest, RiskScore
from wealth_advisor.tools.client_data import LoadClientDataTool, InMemoryClientDataSource, normalize_client_record
from wealth_advisor.tools.crm import HTTPCRMBackend
from wealth_advisor.tools.narrative import NarrativeTool, Narrator, TemplateNarrator, guardrail_violations

FX = {"USD": 1.0, "EUR": 1.1}


def test_normalize_converts_currency_and_drops_bad_rows():
    rec = {"client_id": "X", "as_of": "2026-09-30", "transactions": [
        {"txn_id": "a", "timestamp": "2026-09-01T10:00:00", "amount": -100, "currency": "EUR"},
        {"txn_id": "b", "timestamp": "2026-09-01T10:00:00", "amount": float("nan")},
        {"txn_id": "c", "timestamp": "2026-10-05T10:00:00", "amount": -5},
        {"txn_id": "a", "timestamp": "2026-09-01T10:00:00", "amount": -100, "currency": "EUR"},
    ]}
    out = normalize_client_record(rec, FX)
    assert [t.amount for t in out["transactions"]] == [-110.0]
    assert {i.code for i in out["issues"]} == {"invalid_amount", "future_dated", "duplicate_record"}


def test_loader_rejects_mismatched_or_malformed_records():
    src = InMemoryClientDataSource({"X": {"client_id": "Y", "transactions": []},
                                    "Z": {"client_id": "Z"}})
    tool = LoadClientDataTool(src)
    with pytest.raises(SchemaValidationError):
        tool.run("X")
    with pytest.raises(SchemaValidationError):
        tool.run("Z")
    with pytest.raises(ResourceNotFoundError):
        tool.run("missing")


@pytest.mark.parametrize("store_factory", [InMemoryLongTermMemory, lambda: SQLiteLongTermMemory(":memory:")])
def test_long_term_memory_contract(store_factory):
    m = store_factory()
    m.save_run_summary("C1", "r1", {"risk": {"value": 0.4, "band": "moderate"}, "fingerprints": ["f1"]})
    m.save_run_summary("C1", "r2", {"risk": {"value": 0.5, "band": "elevated"}, "fingerprints": ["f2"]})
    assert m.last_run_summary("C1")["run_id"] == "r2"
    assert m.seen_fingerprints("C1") == {"f1", "f2"}
    m.record_feedback("C1", "f1", "dismiss", "ann", "fp")
    assert "f1" in m.dismissed_fingerprints("C1")
    m.record_feedback("C1", "f1", "confirm", "bob")  # later confirmation reverses the dismissal
    assert m.dismissed_fingerprints("C1") == {}
    m.save_crm_snapshot("C1", {"risk_profile": "moderate"})
    assert m.get_crm_snapshot("C1")[0] == {"risk_profile": "moderate"}
    assert m.last_run_summary("other") is None


FACTS = {"client_first_name": "Ana", "risk_band": "high", "risk_score_pct": 72, "risk_score_scale": 100,
         "previous_risk_score_pct": None, "top_findings": [{"title": "KYC expired 46 days ago", "severity": "high"}],
         "recommendations": [{"priority": "P1", "action": "Refresh KYC."}], "data_caveats": []}


def test_guardrail_rejects_hallucinated_numbers_and_promises():
    assert guardrail_violations("Ana is high risk (72/100); KYC expired 46 days ago.", FACTS) == []
    assert any("number" in p for p in guardrail_violations("Expect 12% returns.", FACTS))
    assert any("guarantee" in p for p in guardrail_violations("We guarantee safety.", FACTS))


def test_narrative_tool_rejects_bad_llm_output():
    class Hallucinating(Narrator):
        source = "llm"

        def narrate(self, facts):
            return "Ana should move $250,000 into bonds."

    with pytest.raises(SchemaValidationError):
        NarrativeTool(Hallucinating()).run(facts=FACTS)
    assert NarrativeTool(TemplateNarrator()).run(facts=FACTS)["source"] == "template"


def test_cli_reviewer_collects_decisions():
    from wealth_advisor.models import Anomaly, Severity
    a = Anomaly("dup", "transaction", Severity.MEDIUM, 0.7, "Dup", "desc", {"key": "k"})
    answers = iter(["e", "d", "merchant refunded", "notes"])
    rev = CLIReviewer(input_fn=lambda _: next(answers), output_fn=lambda _: None, reviewer="ann")
    d = rev.review(ReviewRequest("r", "C1", "Client", ["x"], RiskScore(0.5, "elevated", []), [a]))
    assert d.approved and d.finding_decisions[0].action == "dismiss"
    assert d.finding_decisions[0].note == "merchant refunded"


def test_cli_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "out"
    base = ["--log-level", "CRITICAL", "--log-file", ""]
    assert cli.main(base + ["run", "C005", "--review", "auto", "--out", str(out), "--quiet"]) == 0
    assert cli.main(base + ["run", "C002", "--review", "defer", "--out", str(out), "--quiet"]) == 3
    assert cli.main(base + ["run", "C999", "--review", "auto", "--out", str(out), "--quiet"]) == 1
    pending = [p for p in out.glob("C002_*.json")]
    run_id = json.loads(pending[0].read_text())["run_id"]
    decision = tmp_path / "d.json"
    decision.write_text(json.dumps({"approved": True, "reviewer": "qa"}))
    assert cli.main(base + ["resume", run_id, "--decision", str(decision), "--out", str(out), "--quiet"]) == 0


class _CRMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/clients/C1":
            body = json.dumps({"client_id": "C1", "risk_profile": "moderate", "kyc_status": "verified",
                               "kyc_expiry": "2027-01-01"}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/clients/BUSY":
            self.send_response(503)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def test_http_crm_backend_maps_status_codes():
    server = HTTPServer(("127.0.0.1", 0), _CRMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend = HTTPCRMBackend(f"http://127.0.0.1:{server.server_port}")
        assert backend.get_client("C1")["risk_profile"] == "moderate"
        with pytest.raises(ResourceNotFoundError):
            backend.get_client("NOPE")
        with pytest.raises(TransientToolError):
            backend.get_client("BUSY")
    finally:
        server.shutdown()


class _LLMHandler(BaseHTTPRequestHandler):
    reply = "Ana is high risk (72/100); refresh KYC."

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(length))
        assert req["messages"][0]["role"] == "system"
        body = json.dumps({"choices": [{"message": {"content": self.reply}}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_llm_narrator_openai_compatible_endpoint_and_fallback():
    from wealth_advisor.tools.narrative import LLMNarrator
    server = HTTPServer(("127.0.0.1", 0), _LLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        out = NarrativeTool(LLMNarrator("test-model", base, api_key=""), name="narrative.llm").run(facts=FACTS)
        assert out == {"text": _LLMHandler.reply, "source": "llm"}
        _LLMHandler.reply = "Ana will earn 9% next year."
        with pytest.raises(SchemaValidationError):  # hallucinated number -> rejected -> agent uses template
            NarrativeTool(LLMNarrator("test-model", base, api_key="")).run(facts=FACTS)
    finally:
        server.shutdown()
    with pytest.raises(TransientToolError):  # unreachable endpoint is retryable, then falls back
        LLMNarrator("m", "http://127.0.0.1:9/v1", api_key="", timeout_s=1).narrate(FACTS)


def test_advisor_falls_back_to_template_when_llm_unreachable(settings):
    from wealth_advisor.app import build_system
    from wealth_advisor.hitl import AutoApproveReviewer
    s = settings.with_overrides(llm_enabled=True, llm_base_url="http://127.0.0.1:9/v1", llm_timeout_s=1)
    r = build_system(s, reviewer=AutoApproveReviewer(), sleep=lambda x: None).orchestrator.run("C005")
    assert r.narrative_source == "template" and r.narrative
    advise = next(t for t in r.execution_trace if t.step == "advise")
    assert any("template" in w for w in advise.warnings)
