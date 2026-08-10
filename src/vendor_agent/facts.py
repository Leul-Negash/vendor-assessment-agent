"""Turning retrieved evidence into material facts.

Rules must never be evaluated against raw tool output. Between the two sits
this module, which decides for each material fact whether it is resolved,
missing, only supported by outdated evidence, or contested by two equally
authoritative current sources. The policy's source-priority section is
implemented here, not in the rule table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .schemas import Evidence

_MIN_DATE = date.min

RISK_DATABASE = "internal_vendor_risk_database"
SECURITY_ASSESSMENT = "approved_security_assessment"

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class FactStatus(str, Enum):
    RESOLVED = "resolved"
    MISSING = "missing"
    OUTDATED_ONLY = "outdated_only"
    CONFLICTED = "conflicted"


@dataclass
class Fact:
    name: str
    status: FactStatus
    value: object = None
    sources: list[Evidence] = field(default_factory=list)
    conflicting_values: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_resolved(self) -> bool:
        return self.status is FactStatus.RESOLVED

    def citations(self) -> list[str]:
        return [e.citation() for e in self.sources]


@dataclass
class ResolvedFacts:
    risk_record: Fact
    security_assessment: Fact
    vendor_status: Fact
    risk_rating: Fact

    def all_citations(self) -> list[str]:
        seen: list[str] = []
        for fact in (self.risk_record, self.security_assessment):
            for citation in fact.citations():
                if citation not in seen:
                    seen.append(citation)
        return seen

    def conflicts(self) -> list[Fact]:
        return [
            f
            for f in (self.vendor_status, self.risk_rating)
            if f.status is FactStatus.CONFLICTED
        ]


def _usable(evidence: list[Evidence], source_type: str) -> list[Evidence]:
    return [e for e in evidence if e.source_type == source_type and e.trusted]


def _record_fact(name: str, candidates: list[Evidence]) -> Fact:
    """A record-level fact: present and current, present but stale, or absent."""
    current = [e for e in candidates if e.is_current]
    if current:
        newest = max(current, key=lambda e: e.document_date or _MIN_DATE)
        return Fact(name=name, status=FactStatus.RESOLVED, value=newest.payload, sources=[newest])
    if candidates:
        newest = max(candidates, key=lambda e: e.document_date or _MIN_DATE)
        return Fact(
            name=name, status=FactStatus.OUTDATED_ONLY, value=newest.payload, sources=[newest]
        )
    return Fact(name=name, status=FactStatus.MISSING)


def _material_fact(name: str, key: str, candidates: list[Evidence]) -> Fact:
    """A fact several sources may speak to.

    A current higher-priority source overrides a lower one. Two current sources
    at the same priority that disagree leave the fact contested, which the rule
    table must not read through.
    """
    stated = [e for e in candidates if e.payload.get(key) is not None]
    current = [e for e in stated if e.is_current]
    if not current:
        if stated:
            newest = max(stated, key=lambda e: e.document_date or _MIN_DATE)
            return Fact(
                name=name,
                status=FactStatus.OUTDATED_ONLY,
                value=newest.payload.get(key),
                sources=[newest],
            )
        return Fact(name=name, status=FactStatus.MISSING)

    top_tier = min(e.authority_tier for e in current)
    authoritative = [e for e in current if e.authority_tier == top_tier]

    grouped: dict[str, list[str]] = {}
    for item in authoritative:
        grouped.setdefault(str(item.payload[key]), []).append(item.source_id)

    if len(grouped) > 1:
        return Fact(
            name=name,
            status=FactStatus.CONFLICTED,
            value=None,
            sources=authoritative,
            conflicting_values=grouped,
        )

    value = authoritative[0].payload[key]
    return Fact(name=name, status=FactStatus.RESOLVED, value=value, sources=authoritative)


def resolve(evidence: list[Evidence]) -> ResolvedFacts:
    risk_rows = _usable(evidence, RISK_DATABASE)
    assessments = _usable(evidence, SECURITY_ASSESSMENT)

    return ResolvedFacts(
        risk_record=_record_fact("risk_record", risk_rows),
        security_assessment=_record_fact("security_assessment", assessments),
        vendor_status=_material_fact("vendor_status", "status", risk_rows),
        risk_rating=_material_fact("risk_rating", "risk_rating", risk_rows + assessments),
    )
