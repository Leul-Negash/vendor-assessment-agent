"""What a planner is allowed to return."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..schemas import ToolCall
from ..state import TaskState


@dataclass
class Plan:
    """One decision by the planner: call a tool, or stop and decide."""

    thought: str
    call: ToolCall | None = None
    step_key: str | None = None
    finalize: bool = False
    is_retry: bool = False
    retry_rationale: str | None = None


class Planner(Protocol):
    name: str

    def propose(self, state: TaskState) -> Plan:
        ...

    def observe(self, state: TaskState, plan: Plan, result) -> None:
        ...

    def on_retry_denied(self, plan: Plan, reason: str) -> None:
        ...
