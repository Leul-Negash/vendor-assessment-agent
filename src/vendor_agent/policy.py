"""The policy: loaded from vendor_policy.md, evaluated as an ordered rule table.

`Policy` is the tier-1 source. It is parsed from the markdown so the retrieval
tool can quote real clauses, and the numeric limits it carries are checked
against Settings at load time so the two can never drift apart silently.

`evaluate()` reads only resolved facts, never raw tool output, and returns every
rule that fired. Choosing between them is `select_action()`'s job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .facts import FactStatus, ResolvedFacts
from .schemas import ACTION_SEVERITY, Action, Evidence, Finding, VendorRequest

POLICY_SOURCE_TYPE = "vendor_policy"
POLICY_AUTHORITY_TIER = 1


@dataclass(frozen=True)
class Clause:
    clause_id: str
    heading: str
    text: str

    def as_evidence(self, version: str) -> Evidence:
        return Evidence(
            source_id=self.clause_id,
            source_type=POLICY_SOURCE_TYPE,
            authority_tier=POLICY_AUTHORITY_TIER,
            is_current=True,
            payload={"version": version, "heading": self.heading, "text": self.text},
        )


class PolicyConsistencyError(RuntimeError):
    pass


class Policy:
    """Structured view of vendor_policy.md."""

    def __init__(self, version: str, clauses: list[Clause], cost_threshold: float, max_age_days: int):
        self.version = version
        self.clauses = clauses
        self.cost_threshold = cost_threshold
        self.max_age_days = max_age_days
        self._by_id = {c.clause_id: c for c in clauses}

    @classmethod
    def load(cls, path: Path) -> "Policy":
        text = path.read_text(encoding="utf-8")

        version_match = re.search(r"\*\*Policy version:\*\*\s*([0-9.]+)", text)
        version = version_match.group(1) if version_match else "unknown"

        clauses: list[Clause] = []
        for heading, body in re.findall(r"^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)", text, re.M | re.S):
            slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
            clauses.append(
                Clause(
                    clause_id=f"POLICY-{version}#{slug}",
                    heading=heading.strip(),
                    text=body.strip(),
                )
            )

        cost_match = re.search(r"cost above USD\s*([0-9,]+)", text, re.I)
        age_match = re.search(r"older than\s*(\d+)\s*days", text, re.I)
        if not cost_match or not age_match:
            raise PolicyConsistencyError("policy document is missing its cost or evidence-age limit")

        return cls(
            version=version,
            clauses=clauses,
            cost_threshold=float(cost_match.group(1).replace(",", "")),
            max_age_days=int(age_match.group(1)),
        )

    def clause(self, slug: str) -> Clause:
        return self._by_id[f"POLICY-{self.version}#{slug}"]

    def clause_id(self, slug: str) -> str:
        return f"POLICY-{self.version}#{slug}"

    def search(self, topic: str | None) -> list[Clause]:
        if not topic:
            return list(self.clauses)
        needle = topic.lower().strip()
        hits = [c for c in self.clauses if needle in c.heading.lower() or needle in c.text.lower()]
        return hits or list(self.clauses)

    def assert_consistent_with(self, settings: Settings) -> None:
        if self.cost_threshold != settings.cost_escalation_threshold:
            raise PolicyConsistencyError(
                f"cost threshold differs: policy {self.cost_threshold} vs settings "
                f"{settings.cost_escalation_threshold}"
            )
        if self.max_age_days != settings.evidence_max_age_days:
            raise PolicyConsistencyError(
                f"evidence age limit differs: policy {self.max_age_days} vs settings "
                f"{settings.evidence_max_age_days}"
            )


# --- pre-screen: rules answerable from the request and the policy alone -------


def prescreen(request: VendorRequest, policy: Policy, settings: Settings) -> list[Finding]:
    findings: list[Finding] = []

    missing = [
        field
        for field in settings.required_request_fields
        if getattr(request, field, None) in (None, "")
    ]
    if missing:
        findings.append(
            Finding(
                rule_id="R1-REQUIRED-FIELDS-MISSING",
                action=Action.REQUEST_INFORMATION,
                detail="Required field(s) absent: " + ", ".join(missing),
                citations=[policy.clause_id("required-information")],
            )
        )
        return findings

    if request.data_type not in settings.allowed_data_types:
        findings.append(
            Finding(
                rule_id="R2-DATA-TYPE-INVALID",
                action=Action.REQUEST_INFORMATION,
                detail=(
                    f"Data type {request.data_type!r} is outside the allowed set "
                    f"({', '.join(settings.allowed_data_types)})"
                ),
                citations=[policy.clause_id("data-types")],
            )
        )
        return findings

    if request.data_type == "restricted":
        findings.append(
            Finding(
                rule_id="R3-DATA-TYPE-RESTRICTED",
                action=Action.ESCALATE,
                detail="Restricted data always requires escalation",
                citations=[policy.clause_id("data-types")],
            )
        )

    if request.cost is not None and request.cost > policy.cost_threshold:
        findings.append(
            Finding(
                rule_id="R4-COST-ABOVE-THRESHOLD",
                action=Action.ESCALATE,
                detail=(
                    f"Cost {settings.currency} {request.cost:,.2f} exceeds the "
                    f"{settings.currency} {policy.cost_threshold:,.2f} limit"
                ),
                citations=[policy.clause_id("cost")],
            )
        )

    return findings


def blocks_further_work(findings: list[Finding]) -> bool:
    """True when no additional evidence could change the outcome.

    Missing information stops the run because the policy makes it decisive on
    its own, and a rejection cannot be outranked. An escalation does not stop
    the run: a rejection is stricter, so the agent keeps looking until a
    rejection has been ruled out.
    """
    return any(
        f.action in (Action.REQUEST_INFORMATION, Action.REJECT) for f in findings if f.action
    )


# --- evidence rules -----------------------------------------------------------


def evaluate(
    request: VendorRequest,
    facts: ResolvedFacts,
    policy: Policy,
    settings: Settings,
    injection_sources: list[str] | None = None,
) -> list[Finding]:
    findings = prescreen(request, policy, settings)
    if any(f.action is Action.REQUEST_INFORMATION for f in findings if f.action):
        return findings

    for fact in facts.conflicts():
        detail = "; ".join(
            f"{value} per {', '.join(sources)}" for value, sources in fact.conflicting_values.items()
        )
        findings.append(
            Finding(
                rule_id="R11-SOURCE-CONFLICT",
                action=Action.ESCALATE,
                detail=f"Current priority-2 sources disagree on {fact.name}: {detail}",
                citations=[policy.clause_id("source-priority"), *fact.citations()],
            )
        )

    if facts.vendor_status.is_resolved and facts.vendor_status.value == "prohibited":
        findings.append(
            Finding(
                rule_id="R5-VENDOR-PROHIBITED",
                action=Action.REJECT,
                detail=f"{request.vendor_name} / {request.product} is a prohibited vendor product",
                citations=[
                    policy.clause_id("vendor-status-and-risk"),
                    *facts.vendor_status.citations(),
                ],
            )
        )

    if facts.risk_rating.is_resolved:
        rating = facts.risk_rating.value
        if rating == "high":
            findings.append(
                Finding(
                    rule_id="R6-RISK-HIGH",
                    action=Action.REJECT,
                    detail="Current vendor risk is high",
                    citations=[
                        policy.clause_id("vendor-status-and-risk"),
                        *facts.risk_rating.citations(),
                    ],
                )
            )
        elif rating == "medium":
            findings.append(
                Finding(
                    rule_id="R7-RISK-MEDIUM",
                    action=Action.ESCALATE,
                    detail="Current vendor risk is medium",
                    citations=[
                        policy.clause_id("vendor-status-and-risk"),
                        *facts.risk_rating.citations(),
                    ],
                )
            )

    findings.extend(_evidence_findings(request, facts, policy, settings))

    for source_id in injection_sources or []:
        findings.append(
            Finding(
                rule_id="R12-UNTRUSTED-INSTRUCTION-IGNORED",
                action=None,
                detail=(
                    f"{source_id} contains instructions addressed to the agent; treated as data "
                    "and excluded from evidence"
                ),
                citations=[policy.clause_id("untrusted-content")],
            )
        )

    if not any(f.action for f in findings):
        findings.append(
            Finding(
                rule_id="R10-ALL-CONDITIONS-MET",
                action=Action.APPROVE,
                detail=(
                    "Request complete, vendor permitted, cost within limit, vendor risk low, "
                    "required evidence current, no unresolved conflict"
                ),
                citations=[policy.clause_id("approval"), *facts.all_citations()],
            )
        )

    return findings


def _evidence_findings(
    request: VendorRequest,
    facts: ResolvedFacts,
    policy: Policy,
    settings: Settings,
) -> list[Finding]:
    """Evidence sufficiency for the requested data type."""
    findings: list[Finding] = []
    clause = policy.clause_id("evidence-requirements")
    record = facts.risk_record

    if record.status is not FactStatus.RESOLVED:
        reason = {
            FactStatus.MISSING: "No vendor-risk record could be retrieved",
            FactStatus.OUTDATED_ONLY: (
                "The only vendor-risk record found is older than "
                f"{settings.evidence_max_age_days} days"
            ),
            FactStatus.CONFLICTED: "The vendor-risk record is contested",
        }[record.status]
        findings.append(
            Finding(
                rule_id="R8-RISK-EVIDENCE-NOT-CURRENT",
                action=Action.ESCALATE,
                detail=reason,
                citations=[clause, *record.citations()],
            )
        )

    if request.data_type != "confidential":
        return findings

    assessment = facts.security_assessment
    if assessment.status is not FactStatus.RESOLVED:
        reason = {
            FactStatus.MISSING: "Confidential data requires an approved security assessment; none found",
            FactStatus.OUTDATED_ONLY: (
                "The only approved security assessment found is older than "
                f"{settings.evidence_max_age_days} days"
            ),
            FactStatus.CONFLICTED: "The approved security assessment is contested",
        }[assessment.status]
        findings.append(
            Finding(
                rule_id="R9-SECURITY-ASSESSMENT-NOT-CURRENT",
                action=Action.ESCALATE,
                detail=reason,
                citations=[clause, *assessment.citations()],
            )
        )
    else:
        result = (assessment.value or {}).get("result")
        if result != "pass":
            findings.append(
                Finding(
                    rule_id="R9B-SECURITY-ASSESSMENT-NOT-PASSED",
                    action=Action.ESCALATE,
                    detail=(
                        "Confidential data requires a security assessment with result 'pass'; "
                        f"the current assessment result is {result!r}"
                    ),
                    citations=[clause, *assessment.citations()],
                )
            )

    if record.status is FactStatus.RESOLVED and facts.risk_rating.is_resolved:
        if facts.risk_rating.value != "low":
            findings.append(
                Finding(
                    rule_id="R9C-CONFIDENTIAL-REQUIRES-LOW-RISK",
                    action=Action.ESCALATE,
                    detail=(
                        "Confidential data requires a current low-risk record; current rating is "
                        f"{facts.risk_rating.value!r}"
                    ),
                    citations=[clause, *facts.risk_rating.citations()],
                )
            )

    return findings


def select_action(findings: list[Finding]) -> Action:
    """The strictest action among the rules that fired."""
    actionable = [f.action for f in findings if f.action]
    if not actionable:
        return Action.ESCALATE
    return max(actionable, key=lambda action: ACTION_SEVERITY[action])


def deciding_findings(findings: list[Finding], action: Action) -> list[Finding]:
    return [f for f in findings if f.action is action]
