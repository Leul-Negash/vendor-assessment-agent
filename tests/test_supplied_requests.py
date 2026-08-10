"""The supplied requests, each scored against the expectation in golden/.

One parametrised case per row of vendor_requests.json, covering the normal path,
missing information, tool timeouts, conflicting sources, outdated evidence,
prompt injection and duplicate submission. The batch runs in order because the
duplicate case depends on the one before it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vendor_agent.agent import VendorAssessmentAgent
from vendor_agent.config import Settings
from vendor_agent.schemas import Action, FinalDecision
from vendor_agent.store import MemoryStore
from vendor_agent.tools.dataset import VendorDataset
from vendor_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "golden" / "expected_decisions.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def batch_results(tmp_path_factory):
    """Run every supplied request once, in the order given."""
    root = tmp_path_factory.mktemp("batch")
    settings = Settings(
        data_dir=ROOT / "data" / "mock_vendor_data",
        run_dir=root / "runs",
        log_dir=root / "logs",
    )
    dataset = VendorDataset(settings)
    agent = VendorAssessmentAgent(
        settings=settings,
        dataset=dataset,
        store=MemoryStore(settings.run_dir, seed_decision_log=dataset.seed_decision_log),
        trace=TraceWriter(settings.log_dir, echo=False),
    )
    runs = [agent.assess(request) for request in dataset.requests]
    return agent, runs


CASES = GOLDEN["cases"]


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[f"{c['sequence']:02d}-{c['request_id']}-{c['expected_action']}" for c in CASES],
)
def test_supplied_request_matches_policy(case, batch_results):
    _, runs = batch_results
    run = runs[case["sequence"] - 1]
    decision = run.decision

    assert decision.request_id == case["request_id"]
    assert decision.action.value == case["expected_action"], case["scenario"]

    for rule_id in case["expected_rules"]:
        assert rule_id in decision.policy_rule_ids, f"{rule_id} did not fire: {case['scenario']}"

    assert decision.stop_reason.value == case["expected_stop_reason"]

    if "expected_missing_fields" in case:
        assert sorted(decision.missing_fields) == sorted(case["expected_missing_fields"])
    if "expected_injection_sources" in case:
        assert sorted(decision.injection_attempts) == sorted(case["expected_injection_sources"])
    if "expected_duplicate" in case:
        assert decision.duplicate_of_existing is case["expected_duplicate"]
    if "expected_min_retries" in case:
        assert decision.retries_used >= case["expected_min_retries"]


def test_every_decision_stays_inside_its_budgets(batch_results):
    agent, runs = batch_results
    for run in runs:
        assert run.decision.steps_used <= agent.settings.max_steps + 1
        for ledger in run.state.attempts.values():
            assert ledger.retries <= agent.settings.max_retries_per_step, ledger.step_key


def test_approvals_cite_a_trusted_priority_two_source(batch_results):
    _, runs = batch_results
    approvals = [r for r in runs if r.decision.action is Action.APPROVE]
    assert approvals, "the supplied set contains approvable requests"
    for run in approvals:
        tiered = [c for c in run.decision.citations if "tier 2" in c]
        assert tiered, f"{run.decision.request_id} approved without priority-2 evidence"


def test_decision_log_holds_one_entry_per_request(batch_results):
    agent, runs = batch_results
    entries = json.loads(agent.store.decision_log_path.read_text(encoding="utf-8"))
    ids = [entry["request_id"] for entry in entries]
    assert len(ids) == len(set(ids)), f"duplicate final actions recorded: {ids}"
    assert set(ids) == {r.decision.request_id for r in runs}


def test_every_decision_is_serialisable_and_revalidates(batch_results):
    _, runs = batch_results
    for run in runs:
        payload = run.decision.model_dump_json()
        assert FinalDecision.model_validate_json(payload) == run.decision
