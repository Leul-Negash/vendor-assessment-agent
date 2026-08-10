"""Every tool must declare a complete contract before the agent may call it."""

from __future__ import annotations

import pytest

from vendor_agent.planner.catalogue import SPECS, catalogue_for_prompt
from vendor_agent.schemas import ToolStatus

TOOL_NAMES = {
    "get_policy",
    "lookup_vendor_risk",
    "search_vendor_documents",
    "compute_cost_assessment",
    "record_final_decision",
}


def test_the_agent_exposes_at_least_three_tools():
    assert len(SPECS) >= 3
    assert {spec.name for spec in SPECS} == TOOL_NAMES


@pytest.mark.parametrize("spec", SPECS, ids=[spec.name for spec in SPECS])
def test_each_tool_declares_a_full_contract(spec):
    assert spec.description.strip()
    assert spec.routes, "a tool must declare at least one approved route"
    assert spec.timeout_seconds > 0
    assert spec.max_retries >= 0
    assert spec.fallback, "a tool must declare what happens after failure"
    assert spec.side_effects, "a tool must declare whether it changes anything"

    schema = spec.args_model.model_json_schema()
    assert schema.get("properties") is not None
    assert spec.args_model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("spec", SPECS, ids=[spec.name for spec in SPECS])
def test_retry_limits_respect_the_policy(spec):
    assert spec.max_retries <= 2, "the policy allows at most two retries after the first attempt"


def test_only_the_approval_tool_has_side_effects():
    writers = [spec.name for spec in SPECS if spec.side_effects != "none"]
    assert writers == ["record_final_decision"]


def test_the_side_effecting_tool_is_idempotent():
    approval = next(spec for spec in SPECS if spec.name == "record_final_decision")
    assert approval.idempotent is True
    assert ToolStatus.NO_RESULTS not in approval.retryable_statuses


def test_the_planner_catalogue_hides_the_approval_tool():
    """The planner gathers evidence; recording the outcome is the loop's job."""
    names = {entry["name"] for entry in catalogue_for_prompt()}
    assert "record_final_decision" not in names
    assert len(names) == len(TOOL_NAMES) - 1


def test_the_catalogue_describes_arguments_the_runtime_will_accept(settings, policy, dataset, store):
    from vendor_agent.tools.implementations import build_runtime

    runtime = build_runtime(settings, policy, dataset, store, run_id="test")
    for entry in catalogue_for_prompt():
        spec = runtime.spec(entry["name"])
        assert set(entry["routes"]) == set(spec.routes)
        assert set(entry["arguments"]) == set(spec.args_model.model_fields)
