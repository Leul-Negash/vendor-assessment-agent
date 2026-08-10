PY ?= .venv/bin/python
export PYTHONPATH = src

.PHONY: help install test batch run web tools screenshots demo clean

help:
	@echo "install      create .venv and install dependencies"
	@echo "test         run the test suite"
	@echo "batch        assess all supplied requests and score against golden/"
	@echo "run ID=VR-007  assess one request with a verbose trace"
	@echo "web          serve the review console on http://127.0.0.1:8000"
	@echo "tools        print the tool catalogue and contracts"
	@echo "screenshots  regenerate docs/screenshots (needs chrome)"
	@echo "demo         batch, then screenshots"
	@echo "clean        remove logs, run artefacts and caches"

install:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest

batch:
	$(PY) scripts/run_batch.py

ID ?= VR-007
run:
	$(PY) -m vendor_agent.cli run $(ID)

web:
	$(PY) -m uvicorn vendor_agent.api:app --reload

tools:
	$(PY) -m vendor_agent.cli tools

screenshots:
	$(PY) scripts/capture_screenshots.py

# Order matters: the batch clears the log directory the screenshots read from.
demo: batch screenshots

clean:
	rm -rf logs runs .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
