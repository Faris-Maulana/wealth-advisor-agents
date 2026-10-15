"""Command-line interface.

  python -m wealth_advisor clients
  python -m wealth_advisor run C002                       # interactive human review
  python -m wealth_advisor run --all --review auto        # batch, policy auto-approval
  python -m wealth_advisor run C001 --review defer        # pause; resume later
  python -m wealth_advisor resume <run_id> --decision decision.json
  python -m wealth_advisor run C002 --crm-fault down      # exercise fallbacks
  python -m wealth_advisor history C002
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .app import Faults, System, build_system
from .config import Settings
from .hitl import AutoApproveReviewer, CLIReviewer, DeferredReviewer, FileReviewer, Reviewer, load_decision
from .logging_setup import configure_logging
from .memory import FileSessionMemory
from .models import AdvisoryReport, RunStatus
from .report import render_markdown
from .tools.faults import FAULT_MODES

EXIT_OK, EXIT_FAILED, EXIT_PENDING = 0, 1, 3


def _reviewer(spec: str, reviewer_name: Optional[str]) -> Reviewer:
    if spec == "interactive":
        return CLIReviewer(reviewer=reviewer_name)
    if spec == "auto":
        return AutoApproveReviewer()
    if spec == "defer":
        return DeferredReviewer()
    if spec.startswith("file:"):
        return FileReviewer(Path(spec[5:]))
    raise SystemExit(f"invalid --review '{spec}' (interactive | auto | defer | file:PATH)")


def _system(args, reviewer: Reviewer) -> System:
    settings = Settings.from_env()
    if getattr(args, "review_severity", None):
        settings = settings.with_overrides(review_min_severity=args.review_severity)
    configure_logging(args.log_level, log_file=Path(args.log_file) if args.log_file else None,
                      json_console=args.json_logs)
    return build_system(
        settings,
        faults=Faults(crm=getattr(args, "crm_fault", "none"), market_data=getattr(args, "market_fault", "none")),
        reviewer=reviewer,
        session=FileSessionMemory(settings.state_dir / "runs"),
        memory_path=settings.state_dir / "memory.db",
    )


def _emit(report: AdvisoryReport, out_dir: Path, quiet: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report.client_id}_{report.run_id}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report.to_dict(), indent=2))
    md = render_markdown(report)
    (out_dir / f"{stem}.md").write_text(md)
    if not quiet:
        print(md)
    print(f"[saved] {out_dir / stem}.json | .md  status={report.status.value}", file=sys.stderr)
    if report.status == RunStatus.AWAITING_REVIEW:
        print(f"[pending] resume with: python -m wealth_advisor resume {report.run_id} --decision <file.json>",
              file=sys.stderr)


def _exit_code(reports: list[AdvisoryReport]) -> int:
    if any(r.status == RunStatus.FAILED for r in reports):
        return EXIT_FAILED
    if any(r.status == RunStatus.AWAITING_REVIEW for r in reports):
        return EXIT_PENDING
    return EXIT_OK


def cmd_run(args) -> int:
    system = _system(args, _reviewer(args.review, args.reviewer))
    ids = system.backends.client_data.list_ids() if args.all else args.client_ids
    if not ids:
        raise SystemExit("provide client id(s) or --all")
    reports = system.orchestrator.run_batch(ids)
    for r in reports:
        _emit(r, Path(args.out), args.quiet)
    if len(reports) > 1:
        print("\nBatch summary:", file=sys.stderr)
        for r in reports:
            risk = f"{r.risk.value:.2f} {r.risk.band}" if r.risk else "-"
            print(f"  {r.client_id:6} {r.status.value:24} risk={risk:16} findings={len(r.anomalies)}",
                  file=sys.stderr)
    return _exit_code(reports)


def cmd_resume(args) -> int:
    system = _system(args, DeferredReviewer())
    decision = load_decision(json.loads(Path(args.decision).read_text()))
    report = system.orchestrator.resume(args.run_id, decision)
    _emit(report, Path(args.out), args.quiet)
    return _exit_code([report])


def cmd_pending(args) -> int:
    system = _system(args, DeferredReviewer())
    for run_id in system.session.list_runs():
        req = system.orchestrator.pending_review(run_id)
        if req:
            print(json.dumps(req, indent=2) if args.verbose else
                  f"{run_id}  client={req['client_id']}  findings={len(req['findings'])}  reasons={req['reasons']}")
    return EXIT_OK


def cmd_history(args) -> int:
    system = _system(args, DeferredReviewer())
    print(json.dumps({"runs": system.long_term.history(args.client_id, args.limit),
                      "dismissed": system.long_term.dismissed_fingerprints(args.client_id)}, indent=2))
    return EXIT_OK


def cmd_clients(args) -> int:
    system = _system(args, DeferredReviewer())
    for cid in system.backends.client_data.list_ids():
        print(cid)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wealth_advisor", description="Wealth Advisor Assistant (multi-agent)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--log-file", default="logs/wealth_advisor.jsonl", help="JSONL log file ('' to disable)")
    p.add_argument("--json-logs", action="store_true", help="JSON logs on the console too")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="analyse one or more clients")
    r.add_argument("client_ids", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument("--review", default="interactive", help="interactive | auto | defer | file:PATH")
    r.add_argument("--reviewer", help="reviewer name for interactive mode")
    r.add_argument("--review-severity", choices=["low", "medium", "high", "critical"])
    r.add_argument("--crm-fault", choices=FAULT_MODES, default="none")
    r.add_argument("--market-fault", choices=FAULT_MODES, default="none")
    r.add_argument("--out", default="output")
    r.add_argument("--quiet", action="store_true", help="do not print the markdown report")
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="resume a run paused for review")
    rs.add_argument("run_id")
    rs.add_argument("--decision", required=True, help="review decision JSON file")
    rs.add_argument("--out", default="output")
    rs.add_argument("--quiet", action="store_true")
    rs.set_defaults(fn=cmd_resume)

    pd = sub.add_parser("pending", help="list runs awaiting human review")
    pd.add_argument("-v", "--verbose", action="store_true")
    pd.set_defaults(fn=cmd_pending)

    h = sub.add_parser("history", help="long-term memory for a client")
    h.add_argument("client_id")
    h.add_argument("--limit", type=int, default=10)
    h.set_defaults(fn=cmd_history)

    c = sub.add_parser("clients", help="list available clients")
    c.set_defaults(fn=cmd_clients)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
