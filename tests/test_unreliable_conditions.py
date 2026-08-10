"""Tool failures, retries, and the limits around them."""

from __future__ import annotations

import pytest

from vendor_agent.schemas import Action, StopReason, ToolCall, ToolStatus
from vendor_agent.state import RetryDenied, TaskState
from vendor_agent.tools.base import ToolRuntime
from vendor_agent.tools.implementations import build_runtime
from vendor_agent.tools.scenarios import ScenarioEngine


@pytest.fixture
def runtime(settings, policy, dataset, store) -> ToolRuntime:
    return build_runtime(settings, policy, dataset, store, run_id="test-run")


def test_timeout_on_primary_route_is_recovered_on_the_backup(agent, dataset):
    """TimeoutLabs: the primary risk route times out, the approved backup answers."""
    run = agent.assess(dataset.request("VR-006"))

    risk_steps = [s for s in run.state.steps if s.call and s.call.tool == "lookup_vendor_risk"]
    assert [s.status for s in risk_steps] == [ToolStatus.TIMEOUT, ToolStatus.SUCCESS]
    assert [s.call.route for s in risk_steps] == ["primary", "backup"]
    assert risk_steps[1].is_retry and risk_steps[1].retry_rationale
    assert run.decision.action is Action.APPROVE
    assert run.decision.retries_used == 1


def test_all_routes_timing_out_escalates_instead_of_guessing(agent, dataset):
    """FailWare: both risk routes time out, so the record cannot be obtained."""
    run = agent.assess(dataset.request("VR-007"))

    risk_steps = [s for s in run.state.steps if s.call and s.call.tool == "lookup_vendor_risk"]
    assert len(risk_steps) == 2
    assert all(s.status is ToolStatus.TIMEOUT for s in risk_steps)
    assert sorted(s.call.route for s in risk_steps) == ["backup", "primary"]

    assert run.decision.action is Action.ESCALATE
    assert run.decision.stop_reason is StopReason.EVIDENCE_UNAVAILABLE
    assert "R8-RISK-EVIDENCE-NOT-CURRENT" in run.decision.policy_rule_ids


def test_a_retry_must_change_route_or_arguments(settings, dataset):
    state = TaskState(request=dataset.request("VR-001"), settings=settings)
    call = ToolCall(tool="lookup_vendor_risk", route="primary", args={"vendor_name": "SafeCloud"})

    state.check_retry("vendor_risk", call)
    state.record_step(thought="first attempt", call=call, step_key="vendor_risk")

    with pytest.raises(RetryDenied) as raised:
        state.check_retry("vendor_risk", call)
    assert raised.value.code == "retry_not_materially_different"

    varied = call.model_copy(update={"route": "backup"})
    state.check_retry("vendor_risk", varied)


def test_retry_budget_stops_at_two_retries(settings, dataset):
    state = TaskState(request=dataset.request("VR-001"), settings=settings)
    base = ToolCall(tool="search_vendor_documents", route="approved_repository", args={"q": "a"})

    for index in range(3):
        call = base.model_copy(update={"args": {"q": f"variant-{index}"}})
        state.check_retry("documents", call)
        state.record_step(thought=f"attempt {index + 1}", call=call, step_key="documents")

    assert state.attempts["documents"].retries == 2
    with pytest.raises(RetryDenied) as raised:
        state.check_retry("documents", base.model_copy(update={"args": {"q": "variant-4"}}))
    assert raised.value.code == "retry_budget_exhausted"


def test_empty_results_walk_the_ladder_and_then_stop(agent, dataset):
    """OldStack: both query variants come back empty, then the retry budget ends it."""
    run = agent.assess(dataset.request("VR-011"))

    doc_steps = [s for s in run.state.steps if s.call and s.call.tool == "search_vendor_documents"]
    assert len(doc_steps) == 3
    assert all(s.status is ToolStatus.NO_RESULTS for s in doc_steps)
    variants = [s.call.args["query_variant"] for s in doc_steps]
    assert variants == ["default", "corrected", "default"]
    assert doc_steps[2].call.route == "document_archive"

    assert any("retry_budget_exhausted" in note for note in run.state.notes)
    assert run.decision.retries_used == 2
    assert run.decision.action is Action.ESCALATE


def test_outdated_evidence_is_not_treated_as_current(agent, dataset):
    run = agent.assess(dataset.request("VR-011"))
    stale = [e for e in run.state.evidence if e.source_id == "RISK-004"]
    assert stale and stale[0].is_current is False
    assert stale[0].age_days > agent.settings.evidence_max_age_days
    assert "R8-RISK-EVIDENCE-NOT-CURRENT" in run.decision.policy_rule_ids


def test_unknown_tool_is_refused_by_the_runtime(runtime):
    result = runtime.invoke(ToolCall(tool="delete_everything", args={}))
    assert result.status is ToolStatus.INVALID_ARGS
    assert "unknown tool" in result.error


def test_unapproved_route_is_refused(runtime):
    result = runtime.invoke(
        ToolCall(tool="lookup_vendor_risk", route="vendor_hotline", args={"vendor_name": "SafeCloud"})
    )
    assert result.status is ToolStatus.INVALID_ARGS
    assert "not approved" in result.error


@pytest.mark.parametrize(
    "args, expected_fragment",
    [
        ({}, "vendor_name"),
        ({"vendor_name": ""}, "at least 1 character"),
        ({"vendor_name": "SafeCloud", "sql": "drop table"}, "Extra inputs"),
    ],
)
def test_invalid_arguments_never_reach_the_backend(runtime, args, expected_fragment):
    result = runtime.invoke(ToolCall(tool="lookup_vendor_risk", route="primary", args=args))
    assert result.status is ToolStatus.INVALID_ARGS
    assert expected_fragment in result.error
    assert result.evidence == []


def test_calculator_refuses_a_currency_it_cannot_convert(runtime):
    result = runtime.invoke(
        ToolCall(tool="compute_cost_assessment", route="local", args={"cost": 500, "currency": "EUR"})
    )
    assert result.status is ToolStatus.INVALID_ARGS
    assert "conversion rate" in result.error


def test_a_backend_that_raises_does_not_end_the_run(runtime, monkeypatch):
    tool = runtime.tools["lookup_vendor_risk"]
    monkeypatch.setattr(
        tool, "call", lambda args, route: (_ for _ in ()).throw(RuntimeError("connection reset"))
    )
    result = runtime.invoke(
        ToolCall(tool="lookup_vendor_risk", route="primary", args={"vendor_name": "SafeCloud"})
    )
    assert result.status is ToolStatus.INVALID_ARGS
    assert "connection reset" in result.error


def test_step_budget_fails_closed_to_escalation(make_agent, dataset):
    agent = make_agent(max_steps=2)
    run = agent.assess(dataset.request("VR-001"))

    assert run.decision.stop_reason is StopReason.MAX_STEPS_REACHED
    assert run.decision.action is Action.ESCALATE
    assert "R13-STEP-BUDGET-EXHAUSTED" in run.decision.policy_rule_ids


def test_scenario_rules_match_only_what_they_specify():
    engine = ScenarioEngine(
        [
            {"vendor_name": "TimeoutLabs", "tool": "lookup_vendor_risk", "route": "primary",
             "attempt": 1, "outcome": "timeout"},
            {"vendor_name": "OldStack", "tool": "search_vendor_documents",
             "query_variant": "default", "outcome": "no_results"},
        ]
    )

    assert engine.match("lookup_vendor_risk", "TimeoutLabs", route="primary", route_attempt=1)
    assert engine.match("lookup_vendor_risk", "TimeoutLabs", route="primary", route_attempt=2) is None
    assert engine.match("lookup_vendor_risk", "TimeoutLabs", route="backup", route_attempt=1) is None
    assert engine.match("lookup_vendor_risk", "SafeCloud", route="primary", route_attempt=1) is None

    # Rules that omit a key treat it as a wildcard.
    hit = engine.match(
        "search_vendor_documents", "OldStack", route="document_archive",
        route_attempt=1, query_variant="default",
    )
    assert hit and hit.is_no_results


def test_absent_record_is_an_answer_not_a_failure(agent, extra_requests):
    """A vendor with no record must not be retried into existence."""
    payload = next(r for r in extra_requests["requests"] if r["request_id"] == "EX-004")
    run = agent.assess({k: v for k, v in payload.items() if k != "note"})

    risk_steps = [s for s in run.state.steps if s.call and s.call.tool == "lookup_vendor_risk"]
    assert len(risk_steps) == 1
    assert risk_steps[0].status is ToolStatus.NOT_FOUND
    assert run.decision.action is Action.ESCALATE
    assert "R8-RISK-EVIDENCE-NOT-CURRENT" in run.decision.policy_rule_ids
