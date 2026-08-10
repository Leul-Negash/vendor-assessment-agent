"""Task state: what the agent remembers while a single request is in flight.

This is the short-term memory layer. It holds the step log, the evidence
gathered so far, and the attempt ledger that makes the retry policy
enforceable: a retry is only permitted if the budget allows it *and* the new
attempt differs from every attempt already made for the same step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from secrets import token_hex

from .config import Settings
from .schemas import Evidence, Finding, Step, StopReason, ToolCall, ToolResult, VendorRequest


def _fingerprint(call: ToolCall) -> str:
    payload = json.dumps(
        {"route": call.route, "args": call.args}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass
class StepAttempts:
    """The attempt history for one logical step, e.g. 'retrieve vendor risk'."""

    step_key: str
    fingerprints: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.fingerprints)

    @property
    def retries(self) -> int:
        return max(0, self.count - 1)


class RetryDenied(Exception):
    """Raised when a proposed retry is not permitted."""

    def __init__(self, reason: str, code: str):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class TaskState:
    request: VendorRequest
    settings: Settings
    run_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    steps: list[Step] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    attempts: dict[str, StepAttempts] = field(default_factory=dict)
    injection_sources: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stop_reason: StopReason | None = None
    policy_version: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            # The suffix matters: the same request can legitimately be assessed
            # twice in the same second, and each run needs its own log and row.
            stamp = self.started_at.strftime("%Y%m%dT%H%M%S")
            self.run_id = f"{self.request.request_id}-{stamp}-{token_hex(2)}"

    # --- step bookkeeping ---------------------------------------------------

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def total_retries(self) -> int:
        return sum(a.retries for a in self.attempts.values())

    @property
    def steps_remaining(self) -> int:
        return self.settings.max_steps - self.step_count

    def budget_exhausted(self) -> bool:
        return self.step_count >= self.settings.max_steps

    def check_retry(self, step_key: str, call: ToolCall) -> StepAttempts:
        """Validate a proposed attempt before it is executed.

        Enforces both halves of the policy's retry clause: at most two retries
        after the first attempt, and every retry materially different from what
        has already been tried.
        """
        ledger = self.attempts.setdefault(step_key, StepAttempts(step_key=step_key))
        if ledger.count == 0:
            return ledger

        if ledger.retries >= self.settings.max_retries_per_step:
            raise RetryDenied(
                f"retry budget exhausted for {step_key} "
                f"({self.settings.max_retries_per_step} retries after the first attempt)",
                code="retry_budget_exhausted",
            )

        if _fingerprint(call) in ledger.fingerprints:
            raise RetryDenied(
                f"retry for {step_key} repeats an attempt already made "
                f"(route={call.route}, args={call.args}); a retry must use corrected input, "
                "a corrected query, or a different approved route",
                code="retry_not_materially_different",
            )
        return ledger

    def record_step(
        self,
        thought: str,
        call: ToolCall | None = None,
        result: ToolResult | None = None,
        observation: str = "",
        is_retry: bool = False,
        retry_rationale: str | None = None,
        step_key: str | None = None,
    ) -> Step:
        if call is not None and step_key is not None:
            ledger = self.attempts.setdefault(step_key, StepAttempts(step_key=step_key))
            ledger.fingerprints.append(_fingerprint(call))
            ledger.routes.append(call.route or "default")
            call = call.model_copy(update={"attempt": ledger.count})

        step = Step(
            index=self.step_count + 1,
            thought=thought,
            call=call,
            observation=observation,
            status=result.status if result else None,
            is_retry=is_retry,
            retry_rationale=retry_rationale,
        )
        self.steps.append(step)
        if call and call.tool not in self.tools_called:
            self.tools_called.append(call.tool)
        return step

    # --- evidence ----------------------------------------------------------

    def add_evidence(self, items: list[Evidence]) -> None:
        known = {e.source_id for e in self.evidence}
        for item in items:
            if item.source_id in known:
                continue
            self.evidence.append(item)
            known.add(item.source_id)
            if item.quarantined and item.source_id not in self.injection_sources:
                self.injection_sources.append(item.source_id)

    def trusted_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence if e.trusted]

    def routes_tried(self, step_key: str) -> list[str]:
        ledger = self.attempts.get(step_key)
        return list(ledger.routes) if ledger else []

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "request": self.request.model_dump(mode="json"),
            "started_at": self.started_at.isoformat(),
            "policy_version": self.policy_version,
            "steps": [s.model_dump(mode="json") for s in self.steps],
            "evidence": [e.model_dump(mode="json") for e in self.evidence],
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "attempts": {
                key: {"attempts": led.count, "retries": led.retries, "routes": led.routes}
                for key, led in self.attempts.items()
            },
            "injection_sources": self.injection_sources,
            "tools_called": self.tools_called,
            "notes": self.notes,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
        }


def age_in_days(document_date: date | None, evaluation_date: date) -> int | None:
    if document_date is None:
        return None
    return (evaluation_date - document_date).days
