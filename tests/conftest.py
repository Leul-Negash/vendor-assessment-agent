"""Shared fixtures.

Every test gets its own run directory and log directory, so the memory database
and the decision log never leak between tests. The supplied data directory is
used read-only; tests that need different data copy it first.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vendor_agent.agent import VendorAssessmentAgent
from vendor_agent.config import Settings
from vendor_agent.policy import Policy
from vendor_agent.store import MemoryStore
from vendor_agent.tools.dataset import VendorDataset
from vendor_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parent.parent
SUPPLIED_DATA = ROOT / "data" / "mock_vendor_data"
EXTRA_DATA = ROOT / "data" / "extra_test_data"
GOLDEN = json.loads((ROOT / "golden" / "expected_decisions.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=SUPPLIED_DATA,
        run_dir=tmp_path / "runs",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def dataset(settings: Settings) -> VendorDataset:
    return VendorDataset(settings)


@pytest.fixture
def policy(dataset: VendorDataset) -> Policy:
    return Policy.load(dataset.policy_path)


@pytest.fixture
def store(settings: Settings, dataset: VendorDataset) -> MemoryStore:
    return MemoryStore(settings.run_dir, seed_decision_log=dataset.seed_decision_log)


@pytest.fixture
def make_agent(settings: Settings, dataset: VendorDataset, store: MemoryStore):
    def factory(**overrides) -> VendorAssessmentAgent:
        active = settings if not overrides else Settings(**{**settings.__dict__, **overrides})
        active_dataset = dataset if not overrides else VendorDataset(active)
        return VendorAssessmentAgent(
            settings=active,
            dataset=active_dataset,
            store=store,
            trace=TraceWriter(active.log_dir, echo=False),
        )

    return factory


@pytest.fixture
def agent(make_agent) -> VendorAssessmentAgent:
    return make_agent()


@pytest.fixture
def extra_requests() -> dict:
    return json.loads((EXTRA_DATA / "extra_requests.json").read_text(encoding="utf-8"))


@pytest.fixture
def copied_data_dir(tmp_path: Path) -> Path:
    """A writable copy of the supplied data, for tests that need altered inputs."""
    target = tmp_path / "data"
    shutil.copytree(SUPPLIED_DATA, target)
    return target
