PY ?= python3

.PHONY: install test demo batch api site data clean

install:            ## install with dev + api extras
	$(PY) -m pip install -e ".[dev,api]"

test:               ## run the test suite
	$(PY) -m pytest

demo:               ## interactive human review on the AML scenario
	$(PY) -m wealth_advisor run C002

batch:              ## all clients, policy auto-approval
	$(PY) -m wealth_advisor --log-level WARNING run --all --review auto --quiet

api:                ## REST API on :8000
	uvicorn wealth_advisor.api:app --reload

site:               ## build the static browser demo into site/
	$(PY) scripts/build_site.py

data:               ## regenerate deterministic mock data
	$(PY) scripts/generate_mock_data.py

clean:
	rm -rf .state logs output site .pytest_cache
