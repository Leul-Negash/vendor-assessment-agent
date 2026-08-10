"""Strict schemas for every value that crosses a boundary.

Three boundaries are validated: the request coming in, each tool call and its
result, and the final decision going out. Nothing reaches the decision log
without passing through a model defined here.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Action(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"


# Ordered least to most restrictive. When several policy rules fire, the
# strictest action wins; see policy.select_action().
ACTION_SEVERITY: dict[Action, int] = {
    Action.APPROVE: 0,
    Action.REQUEST_INFORMATION: 1,
    Action.ESCALATE: 2,
    Action.REJECT: 3,
}


class StopReason(str, Enum):
    GOAL_COMPLETE = "goal_complete"
    MISSING_INFORMATION = "missing_information"
    RISK_TOO_HIGH = "risk_too_high"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    DUPLICATE_REQUEST = "duplicate_request"
    MAX_STEPS_REACHED = "max_steps_reached"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    NO_RESULTS = "no_results"
    TIMEOUT = "timeout"
    INVALID_ARGS = "invalid_args"
    NOT_FOUND = "not_found"
    RETURNED_EXISTING = "returned_existing"


class VendorRequest(BaseModel):
    """A request as received. Fields stay optional so that an incomplete
    request is a decidable input rather than a crash."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    vendor_name: str | None = None
    product: str | None = None
    cost: float | None = None
    intended_use: str | None = None
    data_type: str | None = None

    @field_validator("vendor_name", "product", "intended_use", "data_type", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _reject_unparsable_cost(cls, value: Any) -> Any:
        if value is None or isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            text = value.replace(",", "").replace("$", "").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return "__invalid__"
        return "__invalid__"

    @field_validator("cost")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("cost must not be negative")
        return value


class Evidence(BaseModel):
    """One retrieved fact with the provenance needed to cite it."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: str
    authority_tier: int = Field(ge=1, le=4)
    document_date: date | None = None
    age_days: int | None = None
    is_current: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)
    quarantined: bool = False
    quarantine_reason: str | None = None

    @property
    def trusted(self) -> bool:
        return self.authority_tier <= 2 and not self.quarantined

    def citation(self) -> str:
        return f"{self.source_id} ({self.source_type}, tier {self.authority_tier})"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    route: str | None = None
    attempt: int = Field(default=1, ge=1)
    purpose: str = ""


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    route: str | None = None
    status: ToolStatus
    evidence: list[Evidence] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (ToolStatus.SUCCESS, ToolStatus.RETURNED_EXISTING)

    @property
    def retryable(self) -> bool:
        return self.status in (ToolStatus.TIMEOUT, ToolStatus.NO_RESULTS, ToolStatus.INVALID_ARGS)


class Finding(BaseModel):
    """A policy rule that fired, with the evidence that made it fire.

    `action` is None for rules that must be reported but do not by themselves
    drive an outcome, such as an ignored injection attempt.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    action: Action | None = None
    detail: str
    citations: list[str] = Field(default_factory=list)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    thought: str
    call: ToolCall | None = None
    observation: str = ""
    status: ToolStatus | None = None
    is_retry: bool = False
    retry_rationale: str | None = None


class FinalDecision(BaseModel):
    """The only object the agent is allowed to emit as an outcome.

    The validators here are the last gate before the decision is recorded: an
    APPROVE that is not backed by a trusted citation, or any decision without a
    policy rule behind it, is rejected rather than logged.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    action: Action
    rationale: str = Field(min_length=1)
    policy_rule_ids: list[str] = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    stop_reason: StopReason
    steps_used: int = Field(ge=0)
    retries_used: int = Field(ge=0)
    tools_called: list[str] = Field(default_factory=list)
    injection_attempts: list[str] = Field(default_factory=list)
    evaluation_date: date
    duplicate_of_existing: bool = False
    schema_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def _check_action_preconditions(self) -> "FinalDecision":
        if self.action is Action.APPROVE and not self.citations:
            raise ValueError("APPROVE requires at least one citation")
        if self.action is Action.REQUEST_INFORMATION and not self.missing_fields:
            raise ValueError("REQUEST_INFORMATION requires at least one missing field")
        if self.action is not Action.REQUEST_INFORMATION and self.missing_fields:
            raise ValueError("missing_fields must be empty unless information is being requested")
        return self
