"""The default planner: a goal checklist plus an explicit fallback ladder.

The planner decides what to do next; it never decides the outcome. It works
through an ordered list of evidence goals and stops early only when no further
evidence could change the answer — when required information is missing, or
when a rejection has already been established.

Recovery follows a fixed ladder, chosen by *why* the step failed:

  timeout      -> a different approved route
  no results   -> a corrected query, then a different approved route
  invalid args -> corrected input (drop the narrowing filter)
  not found    -> no retry; a definitive absence is an observation, not a failure

Every rung produces an attempt that differs from the ones before it, which is
what the policy requires of a retry. When the ladder runs out the step is
marked blocked, and the missing evidence becomes an escalation at decision time
rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..facts import resolve
from ..policy import Policy, blocks_further_work, evaluate, prescreen
from ..schemas import Action, ToolCall, ToolResult, ToolStatus
from ..state import TaskState
from .base import Plan

POLICY_STEP = "policy"
RISK_STEP = "vendor_risk"
DOCUMENTS_STEP = "documents"
COST_STEP = "cost"


@dataclass
class RulePlanner:
    policy: Policy
    settings: Settings
    name: str = "rule"

    completed: set[str] = field(default_factory=set)
    blocked: dict[str, str] = field(default_factory=dict)
    last_result: dict[str, ToolResult] = field(default_factory=dict)
    last_call: dict[str, ToolCall] = field(default_factory=dict)
    tried_routes: dict[str, list[str]] = field(default_factory=dict)

    # --- main entry point ---------------------------------------------------

    def propose(self, state: TaskState) -> Plan:
        if POLICY_STEP not in self.settled:
            return self._advance(
                state,
                POLICY_STEP,
                "The policy is the highest-priority source, so it is retrieved before any "
                "judgement is formed.",
                ToolCall(
                    tool="get_policy",
                    route="policy_store",
                    args={"topic": None},
                    purpose="load the current policy and its limits",
                ),
            )

        gaps = prescreen(state.request, self.policy, self.settings)
        if any(f.action is Action.REQUEST_INFORMATION for f in gaps if f.action):
            return Plan(
                thought=(
                    "The request is missing required information. No tool can supply what the "
                    "requester did not provide, so the run stops and asks for it."
                ),
                finalize=True,
            )

        if RISK_STEP not in self.settled:
            return self._advance(
                state,
                RISK_STEP,
                f"{state.request.vendor_name} needs a current vendor-risk record before status "
                "and rating can be judged.",
                ToolCall(
                    tool="lookup_vendor_risk",
                    route="primary",
                    args={
                        "vendor_name": state.request.vendor_name,
                        "product": state.request.product,
                    },
                    purpose="retrieve the internal vendor-risk record",
                ),
            )

        blocking = self._blocking_findings(state)
        if blocking:
            return Plan(
                thought=(
                    "A rejection is established and nothing stricter exists, so further "
                    f"evidence cannot change the outcome ({blocking})."
                ),
                finalize=True,
            )

        if DOCUMENTS_STEP not in self.settled:
            return self._advance(
                state,
                DOCUMENTS_STEP,
                "The approved document repository is the second priority-2 source; it is "
                "checked both for the assessment the data type requires and to detect "
                "disagreement with the risk record.",
                ToolCall(
                    tool="search_vendor_documents",
                    route="approved_repository",
                    args={
                        "vendor_name": state.request.vendor_name,
                        "product": state.request.product,
                        "query_variant": "default",
                    },
                    purpose="find security assessments and vendor documents",
                ),
            )

        if COST_STEP not in self.settled:
            ages = [
                e.age_days
                for e in state.trusted_evidence()
                if e.age_days is not None and e.source_type != "vendor_policy"
            ]
            return self._advance(
                state,
                COST_STEP,
                "Cost and evidence age are checked against the policy limits by calculation "
                "rather than by estimate.",
                ToolCall(
                    tool="compute_cost_assessment",
                    route="local",
                    args={
                        "cost": state.request.cost,
                        "currency": self.settings.currency,
                        "evidence_ages_days": ages,
                    },
                    purpose="compare cost and evidence age against policy limits",
                ),
            )

        return Plan(
            thought="Every evidence goal is settled; the policy can now be applied in full.",
            finalize=True,
        )

    # --- goal bookkeeping ---------------------------------------------------

    @property
    def settled(self) -> set[str]:
        return self.completed | set(self.blocked)

    def _advance(self, state: TaskState, step_key: str, thought: str, first_call: ToolCall) -> Plan:
        previous = self.last_result.get(step_key)
        if previous is None:
            return Plan(thought=thought, call=first_call, step_key=step_key)

        recovery = self._recover(step_key, previous)
        if recovery is None:
            self.blocked[step_key] = previous.error or previous.status.value
            return self.propose(state)

        call, rationale = recovery
        return Plan(
            thought=(
                f"The previous attempt returned {previous.status.value}. "
                f"{rationale} This is retry {state.attempts[step_key].retries + 1} of "
                f"{self.settings.max_retries_per_step}."
            ),
            call=call,
            step_key=step_key,
            is_retry=True,
            retry_rationale=rationale,
        )

    def _recover(self, step_key: str, previous: ToolResult) -> tuple[ToolCall, str] | None:
        """The fallback ladder. None means no rung is left."""
        call = self.last_call[step_key]
        spec_routes = {
            "lookup_vendor_risk": ("primary", "backup"),
            "search_vendor_documents": ("approved_repository", "document_archive"),
        }.get(call.tool, (call.route,))
        tried_routes = set(self.tried_routes.get(step_key, []))

        if previous.status is ToolStatus.TIMEOUT:
            for route in spec_routes:
                if route not in tried_routes:
                    return (
                        call.model_copy(update={"route": route}),
                        f"The {call.route} route timed out, so the approved {route} route is "
                        "used instead.",
                    )
            return None

        if previous.status is ToolStatus.NO_RESULTS:
            if call.args.get("query_variant") == "default":
                return (
                    call.model_copy(update={"args": {**call.args, "query_variant": "corrected"}}),
                    "The default query matched nothing, so the query is corrected to a broader "
                    "variant.",
                )
            for route in spec_routes:
                if route not in tried_routes:
                    return (
                        call.model_copy(
                            update={
                                "route": route,
                                "args": {**call.args, "query_variant": "default"},
                            }
                        ),
                        f"Both query variants came back empty on {call.route}, so the approved "
                        f"{route} route is searched.",
                    )
            return None

        if previous.status is ToolStatus.INVALID_ARGS and call.args.get("product") is not None:
            return (
                call.model_copy(update={"args": {**call.args, "product": None}}),
                "The arguments were rejected, so the product filter is dropped to correct the "
                "input.",
            )

        return None

    def observe(self, state: TaskState, plan: Plan, result: ToolResult) -> None:
        step_key = plan.step_key
        if step_key is None or plan.call is None:
            return
        self.last_result[step_key] = result
        self.last_call[step_key] = plan.call
        self.tried_routes.setdefault(step_key, []).append(plan.call.route or "default")

        if result.ok:
            self.completed.add(step_key)
        elif result.status is ToolStatus.NOT_FOUND:
            # An authoritative "no such record" is an answer, not a failure.
            self.completed.add(step_key)
            state.notes.append(
                f"{step_key}: {result.error}; treated as a definitive absence, not retried"
            )

    def on_retry_denied(self, plan: Plan, reason: str) -> None:
        if plan.step_key:
            self.blocked[plan.step_key] = reason

    # --- helpers ------------------------------------------------------------

    def _blocking_findings(self, state: TaskState) -> str:
        facts = resolve(state.trusted_evidence())
        findings = evaluate(
            state.request, facts, self.policy, self.settings, state.injection_sources
        )
        rejects = [f for f in findings if f.action is Action.REJECT]
        if rejects and blocks_further_work(findings):
            return "; ".join(f"{f.rule_id}: {f.detail}" for f in rejects)
        return ""
