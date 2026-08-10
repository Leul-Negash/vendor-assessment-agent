"""The policy engine: precedence, boundaries, conflict resolution, output validation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from vendor_agent.facts import FactStatus, resolve
from vendor_agent.policy import Policy, PolicyConsistencyError, evaluate, prescreen, select_action
from vendor_agent.schemas import (
    Action,
    Evidence,
    FinalDecision,
    Finding,
    StopReason,
    VendorRequest,
)

RISK_DB = "internal_vendor_risk_database"
ASSESSMENT = "approved_security_assessment"


def risk_evidence(source_id="RISK-X", status="approved", rating="low", current=True, tier=2):
    return Evidence(
        source_id=source_id,
        source_type=RISK_DB,
        authority_tier=tier,
        document_date=date(2026, 7, 1),
        age_days=31,
        is_current=current,
        payload={"status": status, "risk_rating": rating},
    )


def assessment_evidence(source_id="SEC-X", result="pass", rating="low", current=True, tier=2):
    return Evidence(
        source_id=source_id,
        source_type=ASSESSMENT,
        authority_tier=tier,
        document_date=date(2026, 7, 2),
        age_days=30,
        is_current=current,
        payload={"result": result, "risk_rating": rating},
    )


def request(**overrides) -> VendorRequest:
    base = dict(
        request_id="T-1",
        vendor_name="TestVendor",
        product="TestProduct",
        cost=1000,
        intended_use="Testing",
        data_type="internal",
    )
    return VendorRequest(**{**base, **overrides})


# --- precedence ---------------------------------------------------------------


def test_the_strictest_applicable_action_wins():
    findings = [
        Finding(rule_id="a", action=Action.ESCALATE, detail=""),
        Finding(rule_id="b", action=Action.REJECT, detail=""),
        Finding(rule_id="c", action=Action.APPROVE, detail=""),
    ]
    assert select_action(findings) is Action.REJECT


def test_informational_findings_do_not_drive_the_action():
    findings = [
        Finding(rule_id="info", action=None, detail=""),
        Finding(rule_id="ok", action=Action.APPROVE, detail=""),
    ]
    assert select_action(findings) is Action.APPROVE


def test_no_applicable_rule_fails_closed():
    assert select_action([]) is Action.ESCALATE


def test_rejection_outranks_escalation_when_both_apply(agent, extra_requests):
    """EX-008: restricted data and an over-limit cost on a prohibited high-risk vendor."""
    payload = next(r for r in extra_requests["requests"] if r["request_id"] == "EX-008")
    run = agent.assess({k: v for k, v in payload.items() if k != "note"})

    assert run.decision.action is Action.REJECT
    assert "R5-VENDOR-PROHIBITED" in run.decision.policy_rule_ids
    assert run.decision.stop_reason is StopReason.RISK_TOO_HIGH


def test_missing_information_takes_precedence_over_evidence_rules(policy, settings):
    findings = prescreen(request(cost=None, data_type=None), policy, settings)
    assert [f.action for f in findings] == [Action.REQUEST_INFORMATION]
    assert "cost" in findings[0].detail and "data_type" in findings[0].detail


# --- boundaries ---------------------------------------------------------------


@pytest.mark.parametrize(
    "cost, escalates",
    [(9999.99, False), (10000, False), (10000.01, True), (15000, True)],
)
def test_cost_threshold_is_exclusive(policy, settings, cost, escalates):
    findings = prescreen(request(cost=cost), policy, settings)
    fired = [f for f in findings if f.rule_id == "R4-COST-ABOVE-THRESHOLD"]
    assert bool(fired) is escalates


@pytest.mark.parametrize(
    "age_days, current",
    [(0, True), (179, True), (180, True), (181, False), (400, False)],
)
def test_evidence_currency_boundary_is_180_days(dataset, age_days, current):
    document_date = dataset.settings.evaluation_date - timedelta(days=age_days)
    computed_age, is_current = dataset._age(document_date)
    assert computed_age == age_days
    assert is_current is current


def test_evaluation_date_comes_from_the_policy_not_the_clock(settings, dataset):
    assert settings.evaluation_date == date(2026, 8, 1)
    assert dataset.scenario_evaluation_date == settings.evaluation_date


def test_policy_limits_are_read_from_the_document(policy):
    assert policy.version == "1.0"
    assert policy.cost_threshold == 10_000.0
    assert policy.max_age_days == 180
    assert policy.clause_id("cost") == "POLICY-1.0#cost"


def test_a_policy_that_disagrees_with_settings_is_rejected(copied_data_dir, settings):
    target = copied_data_dir / "vendor_policy.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("USD 10,000", "USD 25,000"), encoding="utf-8"
    )
    altered = Policy.load(target)
    with pytest.raises(PolicyConsistencyError, match="cost threshold differs"):
        altered.assert_consistent_with(settings)


# --- fact resolution ----------------------------------------------------------


def test_agreeing_sources_resolve_to_one_value():
    facts = resolve([risk_evidence(rating="low"), assessment_evidence(rating="low")])
    assert facts.risk_rating.status is FactStatus.RESOLVED
    assert facts.risk_rating.value == "low"


def test_disagreeing_current_tier_two_sources_leave_the_fact_contested():
    facts = resolve([risk_evidence(rating="low"), assessment_evidence(rating="high")])
    assert facts.risk_rating.status is FactStatus.CONFLICTED
    assert facts.risk_rating.value is None
    assert set(facts.risk_rating.conflicting_values) == {"low", "high"}


def test_a_contested_rating_escalates_rather_than_rejecting(policy, settings):
    facts = resolve([risk_evidence(rating="low"), assessment_evidence(rating="high", result="fail")])
    findings = evaluate(request(data_type="confidential"), facts, policy, settings)
    rules = {f.rule_id for f in findings}

    assert "R11-SOURCE-CONFLICT" in rules
    assert "R6-RISK-HIGH" not in rules, "a contested rating must not be read as a resolved one"
    assert select_action(findings) is Action.ESCALATE


def test_a_higher_priority_source_overrides_a_lower_one():
    vendor_claim = Evidence(
        source_id="VENDOR-X",
        source_type="vendor_document",
        authority_tier=3,
        document_date=date(2026, 7, 30),
        age_days=2,
        payload={"risk_rating": "low", "result": "claimed_pass"},
    )
    facts = resolve([risk_evidence(rating="high"), vendor_claim])
    assert facts.risk_rating.status is FactStatus.RESOLVED
    assert facts.risk_rating.value == "high"


def test_outdated_evidence_does_not_satisfy_a_currency_requirement():
    facts = resolve([risk_evidence(current=False)])
    assert facts.risk_record.status is FactStatus.OUTDATED_ONLY


def test_confidential_data_needs_a_passing_assessment(policy, settings):
    facts = resolve([risk_evidence(), assessment_evidence(result="fail", rating="low")])
    findings = evaluate(request(data_type="confidential"), facts, policy, settings)
    assert "R9B-SECURITY-ASSESSMENT-NOT-PASSED" in {f.rule_id for f in findings}
    assert select_action(findings) is Action.ESCALATE


def test_internal_data_does_not_need_an_assessment(policy, settings):
    facts = resolve([risk_evidence()])
    findings = evaluate(request(data_type="internal"), facts, policy, settings)
    assert select_action(findings) is Action.APPROVE


def test_public_data_needs_only_a_current_risk_record(agent, extra_requests):
    payload = next(r for r in extra_requests["requests"] if r["request_id"] == "EX-007")
    run = agent.assess({k: v for k, v in payload.items() if k != "note"})
    assert run.decision.action is Action.APPROVE


# --- final-output validation --------------------------------------------------


def test_an_approval_without_citations_is_not_a_valid_decision():
    with pytest.raises(ValidationError, match="APPROVE requires at least one citation"):
        FinalDecision(
            request_id="T-1",
            action=Action.APPROVE,
            rationale="Looks fine.",
            policy_rule_ids=["R10-ALL-CONDITIONS-MET"],
            citations=[],
            stop_reason=StopReason.GOAL_COMPLETE,
            steps_used=3,
            retries_used=0,
            evaluation_date=date(2026, 8, 1),
        )


def test_requesting_information_without_naming_a_field_is_invalid():
    with pytest.raises(ValidationError, match="at least one missing field"):
        FinalDecision(
            request_id="T-1",
            action=Action.REQUEST_INFORMATION,
            rationale="Something is missing.",
            policy_rule_ids=["R1-REQUIRED-FIELDS-MISSING"],
            missing_fields=[],
            stop_reason=StopReason.MISSING_INFORMATION,
            steps_used=2,
            retries_used=0,
            evaluation_date=date(2026, 8, 1),
        )


def test_an_unsupported_approval_is_downgraded_to_escalation(agent):
    findings = [Finding(rule_id="R10-ALL-CONDITIONS-MET", action=Action.APPROVE, detail="")]
    facts = resolve([])

    action, revised = agent._validate_evidence_support(Action.APPROVE, findings, facts)

    assert action is Action.ESCALATE
    assert "R14-APPROVAL-UNSUPPORTED" in {f.rule_id for f in revised}
    assert Action.APPROVE not in {f.action for f in revised}


def test_a_decision_always_names_the_rules_it_applied(batch_decisions):
    for decision in batch_decisions:
        assert decision.policy_rule_ids
        assert decision.rationale.strip()


@pytest.fixture
def batch_decisions(agent, dataset):
    return [agent.assess(r).decision for r in dataset.requests]
