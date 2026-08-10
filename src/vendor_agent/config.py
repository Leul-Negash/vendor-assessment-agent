"""Runtime settings for the vendor-assessment agent.

Every limit the agent is bound by lives here, so the control surface is one
file rather than constants scattered through the loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "mock_vendor_data"
DEFAULT_RUN_DIR = PROJECT_ROOT / "runs"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR
    run_dir: Path = DEFAULT_RUN_DIR

    evaluation_date: date = date(2026, 8, 1)
    currency: str = "USD"

    # Policy limits. Mirrored from vendor_policy.md and asserted against the
    # parsed policy at load time by policy.Policy.assert_consistent_with().
    cost_escalation_threshold: float = 10_000.0
    evidence_max_age_days: int = 180

    # Loop guardrails.
    max_steps: int = 14
    max_retries_per_step: int = 2
    tool_timeout_seconds: float = 5.0

    planner: str = "rule"
    llm_model: str = "claude-opus-5"

    required_request_fields: tuple[str, ...] = (
        "vendor_name",
        "product",
        "cost",
        "intended_use",
        "data_type",
    )
    allowed_data_types: tuple[str, ...] = (
        "public",
        "internal",
        "confidential",
        "restricted",
    )
    trusted_authority_tiers: tuple[int, ...] = (1, 2)
    log_dir: Path = field(default=PROJECT_ROOT / "logs")

    def resolved(self) -> "Settings":
        return self

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        """Explicit arguments win; the environment fills in the rest."""
        for field_name, variable, cast in (
            ("data_dir", "VENDOR_AGENT_DATA_DIR", Path),
            ("run_dir", "VENDOR_AGENT_RUN_DIR", Path),
            ("log_dir", "VENDOR_AGENT_LOG_DIR", Path),
            ("planner", "VENDOR_AGENT_PLANNER", str),
            ("max_steps", "VENDOR_AGENT_MAX_STEPS", int),
        ):
            value = os.environ.get(variable)
            if value and field_name not in overrides:
                overrides[field_name] = cast(value)
        return cls(**overrides)


SETTINGS = Settings()
