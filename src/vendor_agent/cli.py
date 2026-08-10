"""Command line entry point.

    python -m vendor_agent.cli run VR-007
    python -m vendor_agent.cli batch
    python -m vendor_agent.cli ask --vendor SafeCloud --product TeamDocs --cost 8000 \
        --use "Store internal documents" --data-type internal
    python -m vendor_agent.cli tools
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import VendorAssessmentAgent
from .config import Settings
from .planner.catalogue import catalogue_table
from .schemas import Action, VendorRequest
from .store import MemoryStore
from .tools.dataset import VendorDataset
from .trace import ACTION_COLOUR, TraceWriter, _COLOURS

ACTION_ORDER = [Action.APPROVE, Action.REQUEST_INFORMATION, Action.ESCALATE, Action.REJECT]


def _paint(text: str, colour: str, enabled: bool) -> str:
    if not enabled or colour not in _COLOURS:
        return text
    return f"{_COLOURS[colour]}{text}{_COLOURS['reset']}"


def _build_agent(args) -> VendorAssessmentAgent:
    settings = Settings.from_env(
        **{
            k: v
            for k, v in {
                "data_dir": Path(args.data_dir) if args.data_dir else None,
                "run_dir": Path(args.run_dir) if args.run_dir else None,
                "planner": args.planner,
                "max_steps": args.max_steps,
            }.items()
            if v is not None
        }
    )
    if getattr(args, "fresh", False):
        for name in ("agent_memory.db", "decision_log.json"):
            target = settings.run_dir / name
            if target.exists():
                target.unlink()

    dataset = VendorDataset(settings)
    store = MemoryStore(settings.run_dir, seed_decision_log=dataset.seed_decision_log)
    trace = TraceWriter(settings.log_dir, echo=not args.quiet)
    return VendorAssessmentAgent(settings=settings, dataset=dataset, store=store, trace=trace)


def _print_summary(runs, colour: bool) -> None:
    counts = {action: 0 for action in ACTION_ORDER}
    for run in runs:
        counts[run.decision.action] += 1
    print("\nsummary")
    for action in ACTION_ORDER:
        label = _paint(f"{action.value:<20}", ACTION_COLOUR.get(action.value, "bold"), colour)
        print(f"  {label} {counts[action]}")
    print(f"  {'total':<20} {len(runs)}")


def cmd_run(args) -> int:
    agent = _build_agent(args)
    request = agent.dataset.request(args.request_id)
    if request is None:
        print(f"no request with id {args.request_id!r} in {agent.settings.data_dir}", file=sys.stderr)
        return 2
    run = agent.assess(request)
    if args.json:
        print(json.dumps(run.to_dict(), indent=2, default=str))
    return 0


def cmd_batch(args) -> int:
    agent = _build_agent(args)
    requests = agent.dataset.requests
    runs = agent.assess_all(requests)
    _print_summary(runs, colour=sys.stdout.isatty())
    print(f"\ntranscript  {agent.trace.transcript_path}")
    print(f"json logs   {agent.trace.runs_dir}")
    print(f"memory      {agent.store.db_path}")
    print(f"decisions   {agent.store.decision_log_path}")
    if args.json:
        print(json.dumps([r.to_dict() for r in runs], indent=2, default=str))
    return 0


def cmd_ask(args) -> int:
    agent = _build_agent(args)
    request = VendorRequest(
        request_id=args.request_id,
        vendor_name=args.vendor,
        product=args.product,
        cost=args.cost,
        intended_use=args.use,
        data_type=args.data_type,
    )
    run = agent.assess(request)
    if args.json:
        print(json.dumps(run.decision.model_dump(mode="json"), indent=2, default=str))
    return 0


def cmd_tools(args) -> int:
    print(json.dumps(catalogue_table(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vendor-agent", description="Vendor-Assessment Agent (ReAct loop over mock tools)"
    )
    parser.add_argument("--data-dir", help="directory holding the mock vendor data")
    parser.add_argument("--run-dir", help="directory for the memory database and decision log")
    parser.add_argument("--planner", choices=("rule", "llm"), help="planner to use (default: rule)")
    parser.add_argument("--max-steps", type=int, help="override the step budget")
    parser.add_argument("--quiet", action="store_true", help="write the transcript without echoing it")
    parser.add_argument("--json", action="store_true", help="also print machine-readable output")
    parser.add_argument(
        "--fresh", action="store_true", help="clear stored decisions before running"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="assess one request from the supplied data")
    run_p.add_argument("request_id")
    run_p.set_defaults(func=cmd_run)

    batch_p = sub.add_parser("batch", help="assess every supplied request in order")
    batch_p.set_defaults(func=cmd_batch)

    ask_p = sub.add_parser("ask", help="assess a request given on the command line")
    ask_p.add_argument("--request-id", default="AD-HOC-1")
    ask_p.add_argument("--vendor")
    ask_p.add_argument("--product")
    ask_p.add_argument("--cost", type=float)
    ask_p.add_argument("--use")
    ask_p.add_argument("--data-type")
    ask_p.set_defaults(func=cmd_ask)

    tools_p = sub.add_parser("tools", help="print the tool catalogue and its contracts")
    tools_p.set_defaults(func=cmd_tools)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
