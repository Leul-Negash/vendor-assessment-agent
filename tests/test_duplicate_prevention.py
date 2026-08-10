"""Duplicate-action prevention, at both layers that enforce it."""

from __future__ import annotations

import json

from vendor_agent.schemas import Action, StopReason, ToolCall, ToolStatus
from vendor_agent.tools.implementations import build_runtime


def test_second_assessment_returns_the_stored_decision(agent, dataset):
    request = dataset.request("VR-010")

    first = agent.assess(request)
    second = agent.assess(request)

    assert first.decision.duplicate_of_existing is False
    assert second.decision.duplicate_of_existing is True
    assert second.decision.stop_reason is StopReason.DUPLICATE_REQUEST
    assert second.decision.action is first.decision.action
    assert second.decision.rationale == first.decision.rationale


def test_the_duplicate_run_stops_before_calling_any_tool(agent, dataset):
    request = dataset.request("VR-010")
    agent.assess(request)
    second = agent.assess(request)

    assert second.state.tools_called == []
    assert len(second.state.steps) == 1
    assert "decision_store hit" in second.state.steps[0].observation


def test_the_decision_log_gains_only_one_entry(agent, dataset):
    request = dataset.request("VR-010")
    agent.assess(request)
    agent.assess(request)
    agent.assess(request)

    entries = json.loads(agent.store.decision_log_path.read_text(encoding="utf-8"))
    matching = [e for e in entries if e["request_id"] == "VR-010"]
    assert len(matching) == 1


def test_the_approval_api_is_idempotent_on_its_own(settings, policy, dataset, store):
    """The storage guard holds even when the upfront memory check is bypassed."""
    runtime = build_runtime(settings, policy, dataset, store, run_id="test-run")
    decision = {
        "request_id": "VR-010",
        "action": "APPROVE",
        "rationale": "Approved for test purposes.",
        "policy_rule_ids": ["R10-ALL-CONDITIONS-MET"],
        "citations": ["RISK-008 (internal_vendor_risk_database, tier 2)"],
        "stop_reason": "goal_complete",
        "steps_used": 5,
        "retries_used": 0,
        "evaluation_date": "2026-08-01",
    }
    call = ToolCall(
        tool="record_final_decision",
        route="approval_api",
        args={"vendor_name": "DuplicateCo", "decision": decision},
    )

    first = runtime.invoke(call)
    second = runtime.invoke(call)

    assert first.status is ToolStatus.SUCCESS
    assert first.data["call_number"] == 1
    assert second.status is ToolStatus.RETURNED_EXISTING
    assert second.data["call_number"] == 2
    assert second.data["was_already_recorded"] is True
    assert second.data["scenario_expected"] == "return_existing"
    assert len(store.all_decisions()) == 1


def test_a_second_submission_cannot_overwrite_the_first(settings, policy, dataset, store):
    runtime = build_runtime(settings, policy, dataset, store, run_id="test-run")
    base = {
        "request_id": "VR-010",
        "rationale": "Recorded first.",
        "policy_rule_ids": ["R10-ALL-CONDITIONS-MET"],
        "citations": ["RISK-008 (internal_vendor_risk_database, tier 2)"],
        "stop_reason": "goal_complete",
        "steps_used": 5,
        "retries_used": 0,
        "evaluation_date": "2026-08-01",
    }
    runtime.invoke(
        ToolCall(
            tool="record_final_decision",
            route="approval_api",
            args={"vendor_name": "DuplicateCo", "decision": {**base, "action": "APPROVE"}},
        )
    )
    runtime.invoke(
        ToolCall(
            tool="record_final_decision",
            route="approval_api",
            args={
                "vendor_name": "DuplicateCo",
                "decision": {**base, "action": "REJECT", "rationale": "Tampered."},
            },
        )
    )

    stored = store.find_decision("VR-010")
    assert stored.action is Action.APPROVE
    assert stored.rationale == "Recorded first."


def test_stored_decisions_survive_a_new_store_instance(settings, dataset, store, make_agent):
    from vendor_agent.store import MemoryStore

    agent = make_agent()
    agent.assess(dataset.request("VR-001"))
    store.close()

    reopened = MemoryStore(settings.run_dir, seed_decision_log=dataset.seed_decision_log)
    assert reopened.find_decision("VR-001") is not None
    assert [row["request_id"] for row in reopened.run_summaries()] == ["VR-001"]
