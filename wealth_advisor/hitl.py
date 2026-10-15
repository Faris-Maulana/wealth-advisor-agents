"""Human-in-the-loop review checkpoint.

The checkpoint sits between analysis and advice: a human validates the
findings *before* they drive recommendations. Reviewers can confirm, dismiss
(false positive -> remembered and suppressed in future runs), or override the
severity of individual findings, or reject the whole run.

Reviewer strategies:
  CLIReviewer        interactive terminal prompt
  DeferredReviewer   pause the run (status=awaiting_review); resume later via
                     `Orchestrator.resume` (API / web UI / `resume` CLI command)
  FileReviewer       decisions from a JSON file (batch / scripted)
  AutoApproveReviewer  policy-based approval, clearly labelled as such
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import FindingDecision, ReviewDecision, ReviewRequest, Severity, from_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Reviewer(ABC):
    @abstractmethod
    def review(self, request: ReviewRequest) -> Optional[ReviewDecision]:
        """Return a decision, or None to defer (run pauses awaiting review)."""


class AutoApproveReviewer(Reviewer):
    def __init__(self, reviewer: str = "auto-policy"):
        self.reviewer = reviewer

    def review(self, request: ReviewRequest) -> ReviewDecision:
        return ReviewDecision(approved=True, reviewer=self.reviewer, decided_at=_now(),
                              notes="Auto-approved by policy (non-interactive run); requires human sign-off "
                                    "before any client-facing action.")


class DeferredReviewer(Reviewer):
    def review(self, request: ReviewRequest) -> None:
        return None


class StaticReviewer(Reviewer):
    """Returns a fixed decision; handy for tests and the API."""

    def __init__(self, decision: ReviewDecision):
        self.decision = decision

    def review(self, request: ReviewRequest) -> ReviewDecision:
        return self.decision


def load_decision(data: dict) -> ReviewDecision:
    decision = from_dict(ReviewDecision, data)
    for fd in decision.finding_decisions:
        if fd.action not in ("confirm", "dismiss", "override"):
            raise ValueError(f"invalid finding action '{fd.action}'")
        if fd.action == "override" and fd.severity is None:
            raise ValueError(f"override of {fd.fingerprint} requires a severity")
    decision.decided_at = decision.decided_at or _now()
    return decision


class FileReviewer(Reviewer):
    def __init__(self, path: Path):
        self.path = Path(path)

    def review(self, request: ReviewRequest) -> ReviewDecision:
        return load_decision(json.loads(self.path.read_text()))


class CLIReviewer(Reviewer):
    def __init__(self, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print,
                 reviewer: Optional[str] = None):
        self.input = input_fn
        self.output = output_fn
        self.reviewer = reviewer

    def review(self, request: ReviewRequest) -> ReviewDecision:
        o = self.output
        o("")
        o("=" * 78)
        o(f" HUMAN REVIEW REQUIRED  run={request.run_id}  client={request.client_id} ({request.client_name})")
        o("=" * 78)
        for r in request.reasons:
            o(f" reason: {r}")
        o(f" risk score: {request.risk.value:.2f} ({request.risk.band})")
        o("")
        for i, f in enumerate(request.findings, 1):
            o(f" [{i}] {f.severity.value.upper():8} {f.title}")
            o(f"      {f.description}")
            o(f"      status={f.status.value} fingerprint={f.fingerprint}")
        o("")
        who = self.reviewer or self.input(" reviewer name: ").strip() or "anonymous"
        while True:
            choice = self.input(" [a]pprove all / [e]dit findings then approve / [r]eject run: ").strip().lower()
            if choice in ("a", "e", "r"):
                break
        decisions: list[FindingDecision] = []
        if choice == "e":
            o(" per finding: Enter=confirm, d=dismiss (false positive), o=override severity")
            for i, f in enumerate(request.findings, 1):
                act = self.input(f"  [{i}] {f.title[:60]} > ").strip().lower()
                if act == "d":
                    note = self.input("      reason for dismissal: ").strip() or None
                    decisions.append(FindingDecision(f.fingerprint, "dismiss", note=note))
                elif act == "o":
                    sev = self._ask_severity()
                    decisions.append(FindingDecision(f.fingerprint, "override", severity=sev))
                else:
                    decisions.append(FindingDecision(f.fingerprint, "confirm"))
        notes = self.input(" notes (optional): ").strip()
        return ReviewDecision(approved=choice != "r", reviewer=who, notes=notes,
                              finding_decisions=decisions, decided_at=_now())

    def _ask_severity(self) -> Severity:
        while True:
            raw = self.input("      new severity (low/medium/high/critical): ").strip().lower()
            try:
                return Severity(raw)
            except ValueError:
                continue
