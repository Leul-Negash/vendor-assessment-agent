#!/usr/bin/env python3
"""Score the agent against golden/expected_decisions.json.

Runs every supplied request in order, compares each outcome with the expectation
derived by hand from the policy, and writes a markdown report next to the logs.

    python scripts/run_batch.py [--planner rule|llm] [--report PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vendor_agent.agent import VendorAssessmentAgent  # noqa: E402
from vendor_agent.config import Settings  # noqa: E402
from vendor_agent.store import MemoryStore  # noqa: E402
from vendor_agent.tools.dataset import VendorDataset  # noqa: E402
from vendor_agent.trace import TraceWriter  # noqa: E402

GOLDEN = ROOT / "golden" / "expected_decisions.json"


def check(case: dict, run) -> list[str]:
    """Return the reasons this case failed, empty if it passed."""
    decision = run.decision
    problems: list[str] = []

    if decision.action.value != case["expected_action"]:
        problems.append(f"action {decision.action.value} != {case['expected_action']}")

    missing_rules = [r for r in case.get("expected_rules", []) if r not in decision.policy_rule_ids]
    if missing_rules:
        problems.append("rules not applied: " + ", ".join(missing_rules))

    expected_stop = case.get("expected_stop_reason")
    if expected_stop and decision.stop_reason.value != expected_stop:
        problems.append(f"stop_reason {decision.stop_reason.value} != {expected_stop}")

    expected_missing = case.get("expected_missing_fields")
    if expected_missing is not None and sorted(decision.missing_fields) != sorted(expected_missing):
        problems.append(f"missing_fields {decision.missing_fields} != {expected_missing}")

    expected_injection = case.get("expected_injection_sources")
    if expected_injection is not None and sorted(decision.injection_attempts) != sorted(
        expected_injection
    ):
        problems.append(f"injection_attempts {decision.injection_attempts} != {expected_injection}")

    if "expected_duplicate" in case and decision.duplicate_of_existing != case["expected_duplicate"]:
        problems.append(
            f"duplicate_of_existing {decision.duplicate_of_existing} != {case['expected_duplicate']}"
        )

    floor = case.get("expected_min_retries")
    if floor is not None and decision.retries_used < floor:
        problems.append(f"retries_used {decision.retries_used} < {floor}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planner", choices=("rule", "llm"), default="rule")
    parser.add_argument("--report", default=str(ROOT / "docs" / "batch_report.md"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = golden["cases"]

    settings = Settings(planner=args.planner)
    if settings.log_dir.exists():
        shutil.rmtree(settings.log_dir)
    for name in ("agent_memory.db", "decision_log.json"):
        stale = settings.run_dir / name
        if stale.exists():
            stale.unlink()

    dataset = VendorDataset(settings)
    store = MemoryStore(settings.run_dir, seed_decision_log=dataset.seed_decision_log)
    trace = TraceWriter(settings.log_dir, echo=not args.quiet)
    agent = VendorAssessmentAgent(settings=settings, dataset=dataset, store=store, trace=trace)

    requests = dataset.requests
    if len(requests) != len(cases):
        print(
            f"warning: {len(requests)} requests supplied but {len(cases)} expectations defined",
            file=sys.stderr,
        )

    rows: list[dict] = []
    passed = 0
    for case, request in zip(cases, requests):
        if request.request_id != case["request_id"]:
            print(
                f"warning: sequence {case['sequence']} expects {case['request_id']} "
                f"but the input holds {request.request_id}",
                file=sys.stderr,
            )
        run = agent.assess(request)
        problems = check(case, run)
        passed += not problems
        rows.append(
            {
                "sequence": case["sequence"],
                "request_id": request.request_id,
                "scenario": case["scenario"],
                "expected": case["expected_action"],
                "actual": run.decision.action.value,
                "rules": ", ".join(run.decision.policy_rule_ids),
                "steps": run.decision.steps_used,
                "retries": run.decision.retries_used,
                "stop": run.decision.stop_reason.value,
                "problems": problems,
            }
        )

    total = len(rows)
    rate = 100.0 * passed / total if total else 0.0

    print(f"\n{passed}/{total} cases matched the expected decision ({rate:.1f}%)")
    for row in rows:
        mark = "pass" if not row["problems"] else "FAIL"
        print(
            f"  {mark}  {row['request_id']:<7} {row['actual']:<20} "
            f"steps={row['steps']:<2} retries={row['retries']}"
            + ("  <- " + "; ".join(row["problems"]) if row["problems"] else "")
        )

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Batch results",
        "",
        f"Planner `{args.planner}`, evaluation date {golden['evaluation_date']}, "
        f"policy v{agent.policy.version}.",
        "",
        f"**{passed} of {total} cases matched the expected decision ({rate:.1f}%).**",
        "",
        "| # | Request | Scenario | Expected | Actual | Rules applied | Steps | Retries | Stop reason |",
        "| - | ------- | -------- | -------- | ------ | ------------- | ----- | ------- | ----------- |",
    ]
    for row in rows:
        actual = row["actual"] if not row["problems"] else f"{row['actual']} (mismatch)"
        lines.append(
            f"| {row['sequence']} | {row['request_id']} | {row['scenario']} | "
            f"{row['expected']} | {actual} | `{row['rules']}` | {row['steps']} | "
            f"{row['retries']} | {row['stop']} |"
        )
    lines += [
        "",
        "`Steps` and `Retries` belong to the decision returned. A duplicate submission "
        "returns the stored decision unchanged, so that row carries the counts of the "
        "original run; the duplicate run itself uses one step and calls no tool.",
        "",
        f"Transcript: `{settings.log_dir.relative_to(ROOT)}/execution_log.txt`  ",
        f"Per-run JSONL: `{settings.log_dir.relative_to(ROOT)}/runs/`  ",
        f"Decision log: `{store.decision_log_path.relative_to(ROOT)}`  ",
        f"Memory database: `{store.db_path.relative_to(ROOT)}`",
        "",
    ]
    if any(row["problems"] for row in rows):
        lines += ["## Mismatches", ""]
        for row in rows:
            if row["problems"]:
                lines.append(f"- **{row['request_id']}**: " + "; ".join(row["problems"]))
        lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nreport      {report}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
