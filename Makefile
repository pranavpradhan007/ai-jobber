# Job-Agent V1 — make targets
# Requires: Python 3.11+, pytest installed (pip install -e .[dev])

PYTHON   ?= python
PYTEST   ?= pytest
SRC      := src
TESTS    := tests

.PHONY: install smoke regression test lint clean push sync

install:
	$(PYTHON) -m pip install -e ".[dev]" --quiet

## smoke — fast happy-path check (seconds)
smoke:
	$(PYTEST) -m smoke --tb=short -q

## regression — full cumulative suite (all sprints)
regression:
	$(PYTEST) --ignore=$(TESTS)/smoke -q

## test — smoke + regression (full gate)
test: smoke regression

## cov — test with coverage report
cov:
	$(PYTEST) --cov=$(SRC) --cov-report=term-missing -q

## lint — basic static checks (no external linter required)
lint:
	$(PYTHON) -m py_compile $(shell find $(SRC) -name "*.py") && echo "Syntax OK"

## push — commit everything and push to GitHub
push:
	git add -A
	git diff --cached --quiet || git commit -m "chore: sync latest changes"
	git push origin main

## sync — tests pass, then push (safer)
sync: test push

## clean — remove caches
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info"   -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Clean."
