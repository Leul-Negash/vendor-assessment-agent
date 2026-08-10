"""HTTP layer: a submission form and a read-only view of what the agent did.

The API is a thin shell over the same agent the CLI uses. It adds no policy
logic of its own, so what a reviewer sees in the browser is what the batch run
produces.

    uvicorn vendor_agent.api:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent import AgentRun, VendorAssessmentAgent
from .config import Settings
from .planner.catalogue import catalogue_table
from .schemas import VendorRequest
from .store import MemoryStore
from .tools.dataset import VendorDataset
from .trace import TraceWriter

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(
    title="Vendor-Assessment Agent",
    description="A bounded ReAct loop over policy-governed tools.",
    version="1.0.0",
)

_settings = Settings.from_env()
_dataset = VendorDataset(_settings)
_store = MemoryStore(_settings.run_dir, seed_decision_log=_dataset.seed_decision_log)


def get_agent() -> VendorAssessmentAgent:
    return VendorAssessmentAgent(
        settings=_settings,
        dataset=_dataset,
        store=_store,
        trace=TraceWriter(_settings.log_dir, echo=False),
    )


class SubmissionForm(BaseModel):
    """What the form posts. Deliberately permissive: an incomplete request is a
    valid submission that the agent answers with REQUEST_INFORMATION."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    vendor_name: str | None = None
    product: str | None = None
    cost: float | str | None = None
    intended_use: str | None = None
    data_type: str | None = None


def serialise(run: AgentRun) -> dict:
    return {
        "decision": run.decision.model_dump(mode="json"),
        "steps": [
            {
                "index": step.index,
                "thought": step.thought,
                "tool": step.call.tool if step.call else None,
                "route": step.call.route if step.call else None,
                "attempt": step.call.attempt if step.call else None,
                "args": step.call.args if step.call else None,
                "observation": step.observation,
                "status": step.status.value if step.status else None,
                "is_retry": step.is_retry,
                "retry_rationale": step.retry_rationale,
            }
            for step in run.state.steps
        ],
        "evidence": [
            {
                "source_id": item.source_id,
                "source_type": item.source_type,
                "authority_tier": item.authority_tier,
                "document_date": item.document_date.isoformat() if item.document_date else None,
                "age_days": item.age_days,
                "is_current": item.is_current,
                "quarantined": item.quarantined,
                "quarantine_reason": item.quarantine_reason,
            }
            for item in run.state.evidence
            if item.source_type != "vendor_policy"
        ],
        "findings": [finding.model_dump(mode="json") for finding in run.state.findings],
        "notes": run.state.notes,
        "run_id": run.state.run_id,
        "limits": {
            "max_steps": run.state.settings.max_steps,
            "max_retries_per_step": run.state.settings.max_retries_per_step,
        },
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/requests")
def list_requests() -> JSONResponse:
    return JSONResponse(
        [
            request.model_dump(mode="json")
            for request in {r.request_id: r for r in _dataset.requests}.values()
        ]
    )


@app.get("/api/tools")
def list_tools() -> JSONResponse:
    return JSONResponse(catalogue_table())


@app.get("/api/policy")
def get_policy() -> JSONResponse:
    agent = get_agent()
    return JSONResponse(
        {
            "version": agent.policy.version,
            "evaluation_date": agent.settings.evaluation_date.isoformat(),
            "cost_threshold": agent.policy.cost_threshold,
            "evidence_max_age_days": agent.policy.max_age_days,
            "clauses": [
                {"clause_id": clause.clause_id, "heading": clause.heading, "text": clause.text}
                for clause in agent.policy.clauses
            ],
        }
    )


@app.post("/api/assess")
def assess(form: SubmissionForm) -> JSONResponse:
    try:
        request = VendorRequest.model_validate(form.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(serialise(get_agent().assess(request)))


@app.post("/api/assess/{request_id}")
def assess_supplied(request_id: str) -> JSONResponse:
    request = _dataset.request(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"no supplied request with id {request_id!r}")
    return JSONResponse(serialise(get_agent().assess(request)))


@app.get("/api/decisions")
def list_decisions() -> JSONResponse:
    return JSONResponse(
        {
            "decisions": [d.model_dump(mode="json") for d in _store.all_decisions()],
            "runs": _store.run_summaries(),
        }
    )
