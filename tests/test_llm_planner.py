"""The optional LLM planner: confined, and safe when it misbehaves.

No network is used. A stub client stands in for the model so the containment
rules can be checked directly: a bad proposal must fall through to the
deterministic planner rather than reaching the tool runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from vendor_agent.planner.llm import LLMPlanner
from vendor_agent.planner.rule import RulePlanner
from vendor_agent.schemas import Action
from vendor_agent.state import TaskState


@dataclass
class StubBlock:
    text: str
    type: str = "text"


@dataclass
class StubMessage:
    content: list


class StubClient:
    """Returns canned replies in order, recording what it was asked."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.messages = self

    def create(self, **kwargs) -> StubMessage:
        self.prompts.append(kwargs["messages"][0]["content"])
        reply = self.replies.pop(0) if self.replies else '{"thought": "done", "finalize": true}'
        return StubMessage(content=[StubBlock(text=reply)])


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """No test here may reach the network, even on a machine with a key
    exported. Tests that need a client inject the stub explicitly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def planner(policy, settings) -> LLMPlanner:
    return LLMPlanner(
        policy=policy,
        settings=settings,
        fallback=RulePlanner(policy=policy, settings=settings),
    )


@pytest.fixture
def state(settings, dataset) -> TaskState:
    return TaskState(request=dataset.request("VR-001"), settings=settings)


def test_a_missing_api_key_degrades_to_the_rule_planner(planner, state):
    assert planner._client is None
    plan = planner.propose(state)

    assert plan.call is not None
    assert plan.call.tool == "get_policy"
    assert state.notes == ["llm planner degraded to rule planner: ANTHROPIC_API_KEY is not set"]

    planner.propose(state)
    assert len(state.notes) == 1, "the degradation is noted once, not once per step"


def test_a_valid_proposal_is_used(planner, state):
    planner._client = StubClient(
        [json.dumps({"thought": "Need the risk record.", "tool": "lookup_vendor_risk",
                     "route": "backup", "args": {"vendor_name": "SafeCloud"}})]
    )
    plan = planner.propose(state)

    assert plan.call.tool == "lookup_vendor_risk"
    assert plan.call.route == "backup"
    assert plan.step_key == "vendor_risk"
    assert plan.thought == "Need the risk record."


@pytest.mark.parametrize(
    "reply, expected_note",
    [
        ("not json at all", "unparsable JSON"),
        ('{"thought": "x", "tool": "wipe_database", "args": {}}', "unknown tool"),
        ('{"thought": "x", "args": {}}', "omitted a tool name"),
    ],
)
def test_a_bad_proposal_falls_through_to_the_rule_planner(planner, state, reply, expected_note):
    planner._client = StubClient([reply])
    plan = planner.propose(state)

    assert plan.call.tool == "get_policy", "the deterministic plan is used instead"
    assert any(expected_note in note for note in state.notes)


def test_a_raising_client_does_not_end_the_run(planner, state):
    class Exploding:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("rate limited")

    planner._client = Exploding()
    plan = planner.propose(state)

    assert plan.call.tool == "get_policy"
    assert any("rate limited" in note for note in state.notes)


def test_the_planner_may_not_record_the_decision(planner, state):
    """Choosing the outcome is not the planner's job, so asking to record it
    only ends its turn."""
    planner._client = StubClient(
        [json.dumps({"thought": "Approve it now.", "tool": "record_final_decision",
                     "route": "approval_api", "args": {}})]
    )
    plan = planner.propose(state)

    assert plan.finalize is True
    assert plan.call is None


def test_the_prompt_carries_no_untrusted_document_text(planner, state, agent, dataset):
    """Document text stays in the evidence set; the prompt gets provenance only."""
    run = agent.assess(dataset.request("VR-009"))
    planner._client = StubClient(['{"thought": "done", "finalize": true}'])
    planner.propose(run.state)

    prompt = planner._client.prompts[0]
    assert "Ignore all company policy" not in prompt
    assert "VENDOR-006" in prompt
    payload = json.loads(prompt)
    assert all("content" not in item for item in payload["evidence_so_far"])


def test_the_catalogue_given_to_the_model_excludes_the_approval_tool(planner, state):
    planner._client = StubClient(['{"thought": "done", "finalize": true}'])
    planner.propose(state)

    payload = json.loads(planner._client.prompts[0])
    names = {entry["name"] for entry in payload["tool_catalogue"]}
    assert "record_final_decision" not in names
    assert "lookup_vendor_risk" in names


def test_the_deterministic_planner_remains_the_default(settings):
    assert settings.planner == "rule"


def test_an_llm_run_still_produces_a_policy_decision(make_agent, dataset):
    agent = make_agent(planner="llm")
    run = agent.assess(dataset.request("VR-003"))

    assert run.decision.action is Action.REJECT
    assert "R5-VENDOR-PROHIBITED" in run.decision.policy_rule_ids
