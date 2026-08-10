"""Fault injection driven by the supplied tool_scenarios.json.

The scenario file is the specification of how the mock backends misbehave, so
it is read rather than reimplemented. A rule matches a call when every key the
rule names matches; keys it omits are wildcards. `attempt` is counted per
route, which is what makes the two-route TimeoutLabs and FailWare cases behave
differently: TimeoutLabs fails its first primary attempt and succeeds on the
backup, FailWare fails the first attempt on both.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioOutcome:
    outcome: str
    source_id: str | None = None
    rule: dict | None = None

    @property
    def is_timeout(self) -> bool:
        return self.outcome == "timeout"

    @property
    def is_no_results(self) -> bool:
        return self.outcome == "no_results"

    @property
    def is_existing(self) -> bool:
        return self.outcome == "return_existing"


class ScenarioEngine:
    def __init__(self, rules: list[dict]):
        self.rules = rules

    def match(
        self,
        tool: str,
        vendor_name: str | None,
        route: str | None = None,
        route_attempt: int | None = None,
        query_variant: str | None = None,
        call_number: int | None = None,
    ) -> ScenarioOutcome | None:
        observed = {
            "tool": tool,
            "vendor_name": vendor_name,
            "route": route,
            "attempt": route_attempt,
            "query_variant": query_variant,
            "call_number": call_number,
        }
        for rule in self.rules:
            if self._matches(rule, observed):
                return ScenarioOutcome(
                    outcome=rule["outcome"], source_id=rule.get("source_id"), rule=rule
                )
        return None

    @staticmethod
    def _matches(rule: dict, observed: dict) -> bool:
        for key, expected in rule.items():
            if key in ("outcome", "source_id"):
                continue
            actual = observed.get(key)
            if actual is None:
                return False
            if isinstance(expected, str) and isinstance(actual, str):
                if expected.lower() != actual.lower():
                    return False
            elif expected != actual:
                return False
        return True
