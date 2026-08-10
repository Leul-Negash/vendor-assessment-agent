"""Read-only access to the supplied mock vendor data.

The files in the data directory are inputs and are never written to. Rows are
converted into `Evidence` here, which is where currency is computed: a record's
age is measured against the policy's evaluation date, not against today's
clock, so runs are reproducible.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path

from ..config import Settings
from ..schemas import Evidence, VendorRequest
from ..sanitize import scan

RISK_DATABASE = "internal_vendor_risk_database"
SECURITY_ASSESSMENT = "approved_security_assessment"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


@dataclass
class VendorDataset:
    settings: Settings

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    def _load_json(self, name: str):
        return json.loads((self.data_dir / name).read_text(encoding="utf-8") or "null")

    @cached_property
    def requests(self) -> list[VendorRequest]:
        return [VendorRequest.model_validate(row) for row in self._load_json("vendor_requests.json")]

    @cached_property
    def risk_rows(self) -> list[dict]:
        with (self.data_dir / "vendor_risk.csv").open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    @cached_property
    def documents(self) -> list[dict]:
        return self._load_json("vendor_documents.json")

    @cached_property
    def scenario_rules(self) -> list[dict]:
        return self._load_json("tool_scenarios.json").get("rules", [])

    @cached_property
    def scenario_evaluation_date(self) -> date | None:
        return _parse_date(self._load_json("tool_scenarios.json").get("evaluation_date"))

    @property
    def seed_decision_log(self) -> Path:
        return self.data_dir / "decision_log.json"

    @property
    def policy_path(self) -> Path:
        return self.data_dir / "vendor_policy.md"

    def request(self, request_id: str) -> VendorRequest | None:
        for item in self.requests:
            if item.request_id == request_id:
                return item
        return None

    # --- evidence construction ---------------------------------------------

    def _age(self, document_date: date | None) -> tuple[int | None, bool]:
        if document_date is None:
            return None, False
        age = (self.settings.evaluation_date - document_date).days
        return age, age <= self.settings.evidence_max_age_days

    def risk_evidence(self, vendor_name: str, product: str | None) -> list[Evidence]:
        out: list[Evidence] = []
        for row in self.risk_rows:
            if row["vendor_name"].lower() != (vendor_name or "").lower():
                continue
            if product and row["product"].lower() != product.lower():
                continue
            assessed = _parse_date(row["assessment_date"])
            age, current = self._age(assessed)
            out.append(
                Evidence(
                    source_id=row["source_id"],
                    source_type=row["source_type"],
                    authority_tier=int(row["authority_tier"]),
                    document_date=assessed,
                    age_days=age,
                    is_current=current,
                    payload={
                        "vendor_id": row["vendor_id"],
                        "vendor_name": row["vendor_name"],
                        "product": row["product"],
                        "status": row["status"],
                        "risk_rating": row["risk_rating"],
                        "assessment_date": row["assessment_date"],
                    },
                )
            )
        return out

    def document_evidence(self, vendor_name: str, product: str | None) -> list[Evidence]:
        """Documents from the approved repository.

        Vendor-supplied material sits at authority tier 3 and its free text is
        untrusted, so it is scanned and quarantined on the way in rather than
        being trusted and filtered later.
        """
        out: list[Evidence] = []
        for row in self.documents:
            if row["vendor_name"].lower() != (vendor_name or "").lower():
                continue
            if product and row["product"].lower() != product.lower():
                continue
            issued = _parse_date(row.get("document_date"))
            age, current = self._age(issued)
            hits = scan(row.get("content"))
            tier = int(row["authority_tier"])
            out.append(
                Evidence(
                    source_id=row["document_id"],
                    source_type=row["source_type"],
                    authority_tier=tier,
                    document_date=issued,
                    age_days=age,
                    is_current=current,
                    quarantined=bool(hits),
                    quarantine_reason=", ".join(hits) if hits else None,
                    payload={
                        "vendor_name": row["vendor_name"],
                        "product": row["product"],
                        "result": row.get("result"),
                        "risk_rating": row.get("risk_rating"),
                        "document_date": row.get("document_date"),
                        "content": row.get("content", ""),
                    },
                )
            )
        return out
