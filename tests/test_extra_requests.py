"""Extra inputs beyond the supplied set, and proof the supplied files are untouched."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vendor_agent.schemas import Action, VendorRequest

SUPPLIED = Path(__file__).resolve().parent.parent / "data" / "mock_vendor_data"


def payload_for(extra_requests: dict, request_id: str) -> dict:
    row = next(r for r in extra_requests["requests"] if r["request_id"] == request_id)
    return {k: v for k, v in row.items() if k != "note"}


@pytest.mark.parametrize(
    "request_id, expected_action",
    [
        ("EX-001", Action.APPROVE),
        ("EX-002", Action.ESCALATE),
        ("EX-003", Action.REQUEST_INFORMATION),
        ("EX-004", Action.ESCALATE),
        ("EX-005", Action.APPROVE),
        ("EX-007", Action.APPROVE),
        ("EX-008", Action.REJECT),
        ("EX-009", Action.REQUEST_INFORMATION),
    ],
)
def test_extra_request_outcomes(agent, extra_requests, request_id, expected_action):
    run = agent.assess(payload_for(extra_requests, request_id))
    assert run.decision.action is expected_action, payload_for(extra_requests, request_id)


def test_a_cost_at_the_threshold_is_not_escalated_on_cost(agent, extra_requests):
    run = agent.assess(payload_for(extra_requests, "EX-001"))
    assert "R4-COST-ABOVE-THRESHOLD" not in run.decision.policy_rule_ids


def test_an_invalid_data_type_asks_for_the_field_rather_than_guessing(agent, extra_requests):
    run = agent.assess(payload_for(extra_requests, "EX-003"))
    assert run.decision.action is Action.REQUEST_INFORMATION
    assert run.decision.missing_fields == ["data_type"]
    assert "R2-DATA-TYPE-INVALID" in run.decision.policy_rule_ids


def test_a_formatted_cost_string_is_coerced(extra_requests):
    request = VendorRequest.model_validate(payload_for(extra_requests, "EX-005"))
    assert request.cost == 8000.0


def test_an_unparsable_cost_is_rejected_by_the_schema(extra_requests):
    with pytest.raises(ValidationError):
        VendorRequest.model_validate(payload_for(extra_requests, "EX-006"))


def test_a_whitespace_only_field_counts_as_missing(agent, extra_requests):
    run = agent.assess(payload_for(extra_requests, "EX-009"))
    assert run.decision.missing_fields == ["vendor_name"]


def test_a_negative_cost_is_rejected():
    with pytest.raises(ValidationError, match="must not be negative"):
        VendorRequest(
            request_id="EX-NEG",
            vendor_name="SafeCloud",
            product="TeamDocs",
            cost=-100,
            intended_use="Testing",
            data_type="internal",
        )


def test_an_unknown_request_field_is_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        VendorRequest(
            request_id="EX-EXTRA",
            vendor_name="SafeCloud",
            product="TeamDocs",
            cost=100,
            intended_use="Testing",
            data_type="internal",
            approved_by_vendor=True,
        )


# --- the supplied data is an input, never an output ---------------------------


def digest_supplied() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(SUPPLIED.iterdir())
        if path.is_file()
    }


def test_a_full_batch_does_not_modify_the_supplied_files(agent, dataset):
    before = digest_supplied()
    for request in dataset.requests:
        agent.assess(request)
    assert digest_supplied() == before


def test_decisions_are_written_to_the_run_directory_instead(agent, dataset):
    agent.assess(dataset.request("VR-001"))

    supplied_log = json.loads((SUPPLIED / "decision_log.json").read_text(encoding="utf-8"))
    assert supplied_log == []

    run_log = json.loads(agent.store.decision_log_path.read_text(encoding="utf-8"))
    assert [entry["request_id"] for entry in run_log] == ["VR-001"]
    assert agent.store.decision_log_path.parent != SUPPLIED
