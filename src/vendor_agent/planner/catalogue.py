"""The tool catalogue as the planner sees it.

Kept separate from the tool implementations so a planner prompt can be built
without constructing backends, and so the description a model reads is exactly
the contract the runtime enforces.
"""

from __future__ import annotations

from ..tools.implementations import (
    ComputeCostAssessment,
    GetPolicy,
    LookupVendorRisk,
    RecordFinalDecision,
    SearchVendorDocuments,
)

SPECS = (
    GetPolicy.spec,
    LookupVendorRisk.spec,
    SearchVendorDocuments.spec,
    ComputeCostAssessment.spec,
    RecordFinalDecision.spec,
)


def catalogue_for_prompt() -> list[dict]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "routes": list(spec.routes),
            "arguments": spec.args_model.model_json_schema().get("properties", {}),
            "timeout_seconds": spec.timeout_seconds,
            "max_retries": spec.max_retries,
        }
        for spec in SPECS
        if spec.name != "record_final_decision"
    ]


def catalogue_table() -> list[dict]:
    return [spec.json_schema() for spec in SPECS]
