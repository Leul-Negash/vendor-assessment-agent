"""The ReAct loop.

Reason, act, observe, update state, repeat, and stop for one of a fixed set of
reasons. The loop owns the guardrails; the planner owns what to try next; the
policy owns the outcome. Those three responsibilities are deliberately not
allowed to blur into each other:

  * a tool failure never propagates as an exception,
  * a retry is checked against the ledger before it is executed,
  * the step budget is bounded and running out fails closed to escalation,
  * the final decision is schema-validated and evidence-checked before it is
    recorded, and recorded at most once per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .facts import ResolvedFacts, resolve
from .policy import Policy, deciding_findings, evaluate, select_action
from .planner.base import Plan, Planner
from .planner.rule import RulePlanner
from .schemas import (
    Action,
    Finding,
    FinalDecision,
    StopReason,
    ToolCall,
    ToolResult,
    ToolStatus,
    VendorRequest,
)
from .state import RetryDenied, TaskState
from .store import MemoryStore
from .tools.base import ToolRuntime
from .tools.dataset import VendorDataset
from .tools.implementations import build_runtime
from .trace import TraceWriter


@dataclass
class AgentRun:
    state: TaskState
    decision: FinalDecision
    facts: ResolvedFacts | None = None
    trace_path: Path | None = None

    @property
    def action(self) -> Action:
        return self.decision.action

    def to_dict(self) -> dict:
        payload = self.state.to_dict()
        payload["decision"] = self.decision.model_dump(mode="json")
        return payload


class VendorAssessmentAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        store: MemoryStore | None = None,
        dataset: VendorDataset | None = None,
        policy: Policy | None = None,
        trace: TraceWriter | None = None,
        planner_factory=None,
    ):
        self.settings = settings or Settings()
        self.dataset = dataset or VendorDataset(self.settings)
        self.policy = policy or Policy.load(self.dataset.policy_path)
        self.policy.assert_consistent_with(self.settings)
        self.store = store or MemoryStore(
            self.settings.run_dir, seed_decision_log=self.dataset.seed_decision_log
        )
        self.trace = trace or TraceWriter(self.settings.log_dir)
        self.planner_factory = planner_factory or self._default_planner_factory

    def _default_planner_factory(self) -> Planner:
        if self.settings.planner == "llm":
            from .planner.llm import LLMPlanner

            return LLMPlanner(
                policy=self.policy,
                settings=self.settings,
                fallback=RulePlanner(policy=self.policy, settings=self.settings),
            )
        return RulePlanner(policy=self.policy, settings=self.settings)

    # --- public API ---------------------------------------------------------

    def assess(self, request: VendorRequest | dict) -> AgentRun:
        if isinstance(request, dict):
            request = VendorRequest.model_validate(request)

        state = TaskState(request=request, settings=self.settings)
        state.policy_version = self.policy.version
        planner = self.planner_factory()
        runtime = build_runtime(
            self.settings, self.policy, self.dataset, self.store, run_id=state.run_id
        )
        self.store.open_run(state)
        self.trace.open(state, planner_name=getattr(planner, "name", "rule"))

        duplicate = self._duplicate_guard(state)
        if duplicate is not None:
            run = AgentRun(state=state, decision=duplicate)
            self._close(run)
            return run

        self._loop(state, planner, runtime)
        run = self._finalize(state, planner, runtime)
        self._close(run)
        return run

    def assess_all(self, requests: list[VendorRequest] | None = None) -> list[AgentRun]:
        return [self.assess(r) for r in (requests if requests is not None else self.dataset.requests)]

    # --- loop ---------------------------------------------------------------

    def _duplicate_guard(self, state: TaskState) -> FinalDecision | None:
        """Long-term memory is consulted before any work is done.

        The approval tool is idempotent as well, so a duplicate is caught even
        if this check is bypassed; catching it here also avoids re-running the
        assessment.
        """
        existing = self.store.find_decision(state.request.request_id)
        if existing is None:
            return None

        state.stop_reason = StopReason.DUPLICATE_REQUEST
        step = state.record_step(
            thought=(
                f"Memory already holds a final action for {state.request.request_id}. The policy "
                "forbids recording the same final action twice, so the stored decision is "
                "returned unchanged."
            ),
            observation=(
                f"decision_store hit: {existing.action.value} recorded previously "
                f"(rule {', '.join(existing.policy_rule_ids)})"
            ),
        )
        self.trace.step(state, step)
        return existing.model_copy(
            update={
                "duplicate_of_existing": True,
                "stop_reason": StopReason.DUPLICATE_REQUEST,
            }
        )

    def _loop(self, state: TaskState, planner: Planner, runtime: ToolRuntime) -> None:
        while True:
            if state.budget_exhausted():
                state.stop_reason = StopReason.MAX_STEPS_REACHED
                return

            plan = planner.propose(state)
            if plan.finalize or plan.call is None:
                state.notes.append(f"planner stopped: {plan.thought}")
                return

            spec = runtime.spec(plan.call.tool)
            try:
                state.check_retry(plan.step_key or plan.call.tool, plan.call)
            except RetryDenied as denied:
                planner.on_retry_denied(plan, denied.reason)
                step = state.record_step(
                    thought=plan.thought,
                    observation=f"retry refused: {denied.reason}",
                )
                state.notes.append(f"{denied.code}: {denied.reason}")
                self.trace.step(state, step)
                continue

            result = runtime.invoke(plan.call)
            step = state.record_step(
                thought=plan.thought,
                call=plan.call,
                result=result,
                observation=self._describe(result, spec.timeout_seconds),
                is_retry=plan.is_retry,
                retry_rationale=plan.retry_rationale,
                step_key=plan.step_key,
            )
            state.add_evidence(result.evidence)
            planner.observe(state, plan, result)
            self.trace.step(state, step)

    @staticmethod
    def _describe(result: ToolResult, timeout: float) -> str:
        if result.status is ToolStatus.TIMEOUT:
            return f"timeout after {result.latency_ms}ms (budget {timeout}s): {result.error}"
        if not result.ok:
            return f"{result.status.value}: {result.error}"

        parts = [result.status.value]
        if result.evidence:
            usable = [e for e in result.evidence if e.trusted]
            quarantined = [e for e in result.evidence if e.quarantined]
            outdated = [e for e in result.evidence if not e.is_current and not e.quarantined]
            parts.append(f"{len(result.evidence)} item(s), {len(usable)} usable")
            if outdated:
                parts.append(
                    "outdated: "
                    + ", ".join(f"{e.source_id} at {e.age_days}d" for e in outdated)
                )
            if quarantined:
                parts.append(
                    "quarantined: "
                    + ", ".join(f"{e.source_id} ({e.quarantine_reason})" for e in quarantined)
                )
        for key in ("exceeds_threshold", "headroom", "recorded_action", "was_already_recorded"):
            if key in result.data:
                parts.append(f"{key}={result.data[key]}")
        parts.append(f"{result.latency_ms}ms")
        return " | ".join(parts)

    # --- decision -----------------------------------------------------------

    def _finalize(self, state: TaskState, planner: Planner, runtime: ToolRuntime) -> AgentRun:
        facts = resolve(state.trusted_evidence())
        findings = evaluate(
            state.request, facts, self.policy, self.settings, state.injection_sources
        )

        if state.stop_reason is StopReason.MAX_STEPS_REACHED:
            findings.append(
                Finding(
                    rule_id="R13-STEP-BUDGET-EXHAUSTED",
                    action=Action.ESCALATE,
                    detail=(
                        f"The step budget of {self.settings.max_steps} was reached before the "
                        "assessment completed; the request is escalated rather than decided on "
                        "incomplete evidence"
                    ),
                    citations=[self.policy.clause_id("retries-and-final-actions")],
                )
            )

        action = select_action(findings)
        action, findings = self._validate_evidence_support(action, findings, facts)
        state.findings = findings

        decision = self._build_decision(state, facts, findings, action)
        decision = self._submit(state, decision, runtime)
        return AgentRun(state=state, decision=decision, facts=facts)

    def _validate_evidence_support(
        self, action: Action, findings: list[Finding], facts: ResolvedFacts
    ) -> tuple[Action, list[Finding]]:
        """An approval must rest on trusted evidence, not on absence of objection."""
        if action is not Action.APPROVE:
            return action, findings
        if facts.all_citations():
            return action, findings

        findings = [f for f in findings if f.action is not Action.APPROVE]
        findings.append(
            Finding(
                rule_id="R14-APPROVAL-UNSUPPORTED",
                action=Action.ESCALATE,
                detail=(
                    "No rule objected, but no trusted priority-2 evidence was gathered either; "
                    "an approval with nothing to cite is refused and the request is escalated"
                ),
                citations=[self.policy.clause_id("approval")],
            )
        )
        return Action.ESCALATE, findings

    def _build_decision(
        self,
        state: TaskState,
        facts: ResolvedFacts,
        findings: list[Finding],
        action: Action,
    ) -> FinalDecision:
        drivers = deciding_findings(findings, action)
        informational = [f for f in findings if f.action is None]

        # The clauses that drove the outcome come first, then every trusted
        # priority-2 record that was consulted, so a reviewer can see both the
        # reason and the evidence base behind it.
        citations: list[str] = []
        for finding in drivers:
            for citation in finding.citations:
                if citation not in citations:
                    citations.append(citation)
        for citation in facts.all_citations():
            if citation not in citations:
                citations.append(citation)

        missing: list[str] = []
        if action is Action.REQUEST_INFORMATION:
            for finding in drivers:
                if finding.rule_id == "R1-REQUIRED-FIELDS-MISSING":
                    missing = [
                        f
                        for f in self.settings.required_request_fields
                        if getattr(state.request, f, None) in (None, "")
                    ]
                elif finding.rule_id == "R2-DATA-TYPE-INVALID":
                    missing = ["data_type"]

        rationale = self._rationale(state, action, drivers, informational)
        state.stop_reason = state.stop_reason or self._stop_reason(action, findings)

        rule_ids = [f.rule_id for f in drivers]
        rule_ids += [f.rule_id for f in findings if f.rule_id not in rule_ids]

        payload = dict(
            request_id=state.request.request_id,
            action=action,
            rationale=rationale,
            policy_rule_ids=rule_ids or ["R13-STEP-BUDGET-EXHAUSTED"],
            citations=citations,
            missing_fields=missing,
            stop_reason=state.stop_reason,
            # The submission itself is the next step; counting it here keeps the
            # stored decision and the returned decision in agreement.
            steps_used=state.step_count + 1,
            retries_used=state.total_retries,
            tools_called=state.tools_called,
            injection_attempts=state.injection_sources,
            evaluation_date=self.settings.evaluation_date,
        )
        try:
            return FinalDecision(**payload)
        except ValidationError as exc:
            # The schema is the last gate. A decision that cannot satisfy it is
            # not emitted as-is; the request escalates with the reason attached.
            state.notes.append(f"final-output validation failed: {exc.errors()}")
            return FinalDecision(
                request_id=state.request.request_id,
                action=Action.ESCALATE,
                rationale=(
                    "The proposed decision failed final-output validation and was replaced with "
                    f"an escalation. Validation error: {exc.error_count()} problem(s)."
                ),
                policy_rule_ids=["R15-OUTPUT-VALIDATION-FAILED"],
                citations=[self.policy.clause_id("approval")],
                stop_reason=StopReason.EVIDENCE_UNAVAILABLE,
                steps_used=state.step_count,
                retries_used=state.total_retries,
                tools_called=state.tools_called,
                injection_attempts=state.injection_sources,
                evaluation_date=self.settings.evaluation_date,
            )

    def _rationale(
        self,
        state: TaskState,
        action: Action,
        drivers: list[Finding],
        informational: list[Finding],
    ) -> str:
        request = state.request
        head = {
            Action.APPROVE: "Approved",
            Action.REJECT: "Rejected",
            Action.ESCALATE: "Escalated",
            Action.REQUEST_INFORMATION: "Information requested",
        }[action]
        subject = f"{request.vendor_name or 'unnamed vendor'} / {request.product or 'unnamed product'}"
        reasons = " ".join(f"{f.detail}." for f in drivers) or "No rule objected."
        extra = " ".join(f"{f.detail}." for f in informational)
        return f"{head}: {subject}. {reasons}{(' ' + extra) if extra else ''}".strip()

    @staticmethod
    def _stop_reason(action: Action, findings: list[Finding]) -> StopReason:
        rule_ids = {f.rule_id for f in findings}
        if action is Action.REQUEST_INFORMATION:
            return StopReason.MISSING_INFORMATION
        if action is Action.REJECT:
            return StopReason.RISK_TOO_HIGH
        if action is Action.ESCALATE and rule_ids & {
            "R8-RISK-EVIDENCE-NOT-CURRENT",
            "R9-SECURITY-ASSESSMENT-NOT-CURRENT",
        }:
            return StopReason.EVIDENCE_UNAVAILABLE
        return StopReason.GOAL_COMPLETE

    def _submit(self, state: TaskState, decision: FinalDecision, runtime: ToolRuntime) -> FinalDecision:
        call = ToolCall(
            tool="record_final_decision",
            route="approval_api",
            args={
                "vendor_name": state.request.vendor_name or "unknown",
                "decision": decision.model_dump(mode="json"),
            },
            purpose="record the final action exactly once",
        )
        thought = (
            f"The policy resolves to {decision.action.value}. Recording it through the approval "
            "API, which is idempotent on request_id."
        )
        try:
            state.check_retry("record", call)
        except RetryDenied as denied:
            step = state.record_step(thought=thought, observation=f"retry refused: {denied.reason}")
            self.trace.step(state, step)
            return decision

        result = runtime.invoke(call)
        step = state.record_step(
            thought=thought,
            call=call,
            result=result,
            observation=self._describe(result, runtime.spec(call.tool).timeout_seconds),
            step_key="record",
        )
        self.trace.step(state, step)

        if result.status is ToolStatus.RETURNED_EXISTING:
            stored = self.store.find_decision(decision.request_id)
            if stored is not None:
                state.stop_reason = StopReason.DUPLICATE_REQUEST
                return stored.model_copy(
                    update={
                        "duplicate_of_existing": True,
                        "stop_reason": StopReason.DUPLICATE_REQUEST,
                    }
                )
        if not result.ok:
            state.notes.append(f"approval API did not acknowledge: {result.error}")
        return decision

    def _close(self, run: AgentRun) -> None:
        self.store.close_run(run.state, run.decision)
        run.trace_path = self.trace.close(run)
