"""Execution logging.

Two views of the same run: a machine-readable JSONL stream per run, for the
batch report and for diffing, and a human-readable transcript that shows the
loop as reason / act / observe so a reviewer can follow what the agent did and
why without reading JSON.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .schemas import Step, ToolStatus
from .state import TaskState

RULE = "─" * 92
HEAVY = "═" * 92

_COLOURS = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}

ACTION_COLOUR = {
    "APPROVE": "green",
    "ESCALATE": "yellow",
    "REQUEST_INFORMATION": "cyan",
    "REJECT": "red",
}

STATUS_COLOUR = {
    ToolStatus.SUCCESS: "green",
    ToolStatus.RETURNED_EXISTING: "cyan",
    ToolStatus.NO_RESULTS: "yellow",
    ToolStatus.NOT_FOUND: "yellow",
    ToolStatus.TIMEOUT: "red",
    ToolStatus.INVALID_ARGS: "red",
}


@dataclass
class TraceWriter:
    log_dir: Path
    echo: bool = False
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    colour: bool | None = None

    def __post_init__(self) -> None:
        self.log_dir = Path(self.log_dir)
        self.runs_dir = self.log_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.log_dir / "execution_log.txt"
        self._jsonl: Path | None = None
        if self.colour is None:
            self.colour = bool(getattr(self.stream, "isatty", lambda: False)())

    # --- helpers ------------------------------------------------------------

    def _paint(self, text: str, colour: str) -> str:
        if not self.colour or colour not in _COLOURS:
            return text
        return f"{_COLOURS[colour]}{text}{_COLOURS['reset']}"

    def _emit(self, line: str, plain: str | None = None) -> None:
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write((plain if plain is not None else line) + "\n")
        if self.echo:
            self.stream.write(line + "\n")
            self.stream.flush()

    def _write_json(self, record: dict) -> None:
        if self._jsonl is None:
            return
        with self._jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    # --- lifecycle ----------------------------------------------------------

    def open(self, state: TaskState, planner_name: str = "rule") -> None:
        self._jsonl = self.runs_dir / f"{state.run_id}.jsonl"
        self._jsonl.write_text("", encoding="utf-8")

        request = state.request
        cost = "missing" if request.cost is None else f"{state.settings.currency} {request.cost:,.2f}"
        header = (
            f"run {state.run_id}  planner={planner_name}  policy v{state.policy_version}  "
            f"evaluation_date={state.settings.evaluation_date}"
        )
        detail = (
            f"request  {request.vendor_name or '—'} / {request.product or '—'}  |  {cost}  |  "
            f"data_type={request.data_type or 'missing'}  |  use={request.intended_use or 'missing'}"
        )
        self._emit(HEAVY)
        self._emit(self._paint(header, "bold"), header)
        self._emit(detail)
        self._emit(RULE)
        self._write_json(
            {
                "event": "run_started",
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": state.run_id,
                "planner": planner_name,
                "policy_version": state.policy_version,
                "request": request.model_dump(mode="json"),
                "limits": {
                    "max_steps": state.settings.max_steps,
                    "max_retries_per_step": state.settings.max_retries_per_step,
                },
            }
        )

    def step(self, state: TaskState, step: Step) -> None:
        tag = f"step {step.index:>2}"
        retry = "  [retry]" if step.is_retry else ""
        self._emit(f"{tag}  reason   {step.thought}{retry}")

        if step.call:
            args = json.dumps(step.call.args, default=str)
            if len(args) > 150:
                args = args[:149] + "…"
            self._emit(
                f"          act      {step.call.tool}(route={step.call.route}, "
                f"attempt={step.call.attempt}) args={args}"
            )
        if step.observation:
            colour = STATUS_COLOUR.get(step.status, "dim") if step.status else "dim"
            plain = f"          observe  {step.observation}"
            self._emit(f"          observe  {self._paint(step.observation, colour)}", plain)
        if step.retry_rationale:
            self._emit(f"          update   retry rationale: {step.retry_rationale}")

        self._write_json(
            {
                "event": "step",
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": state.run_id,
                **step.model_dump(mode="json"),
                "evidence_count": len(state.evidence),
                "retries_used": state.total_retries,
                "steps_remaining": state.steps_remaining,
            }
        )

    def close(self, run) -> Path | None:
        decision = run.decision
        state = run.state
        colour = ACTION_COLOUR.get(decision.action.value, "bold")
        self._emit(RULE)

        headline = (
            f"decision {decision.action.value}   stop={decision.stop_reason.value}   "
            f"steps={decision.steps_used}   retries={decision.retries_used}"
            f"{'   duplicate=true' if decision.duplicate_of_existing else ''}"
        )
        self._emit(
            f"{self._paint(headline, colour)}",
            headline,
        )
        self._emit(f"rules    {', '.join(decision.policy_rule_ids)}")
        self._emit(f"cite     {', '.join(decision.citations) or '—'}")
        if decision.missing_fields:
            self._emit(f"missing  {', '.join(decision.missing_fields)}")
        if decision.injection_attempts:
            self._emit(f"blocked  injection attempt in {', '.join(decision.injection_attempts)}")
        self._emit(f"why      {decision.rationale}")
        for note in state.notes:
            self._emit(f"note     {note}")
        self._emit(HEAVY)
        self._emit("")

        self._write_json(
            {
                "event": "run_finished",
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": state.run_id,
                "decision": decision.model_dump(mode="json"),
                "notes": state.notes,
                "attempts": state.to_dict()["attempts"],
            }
        )
        path, self._jsonl = self._jsonl, None
        return path
