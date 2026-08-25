SHELL := /bin/bash
PYTHON311 := python3.11

ifeq ($(OS),Windows_NT)
	VENV_BIN := .venv/Scripts
	PY := $(VENV_BIN)/python.exe
	PIP := $(VENV_BIN)/pip.exe
else
	VENV_BIN := .venv/bin
	PY := $(VENV_BIN)/python
	PIP := $(VENV_BIN)/pip
endif

.PHONY: setup doctor lint test clean data eval demo approve verify-audit rollback harvest migrate

setup:
	$(PYTHON311) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

doctor:
	$(PY) -m src.config doctor

lint:
	$(PY) -m ruff check src tests

test:
	$(PY) -m pytest -q

clean:
	rm -rf .venv .pytest_cache .ruff_cache second_rail.db second_rail.db-wal second_rail.db-shm
	rm -f cache/*.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

data:
	@echo "not yet built — phase 2 (make data)"

eval:
	@echo "not yet built — phase 7 (make eval)"

demo:
	@echo "not yet built — phase 8/9 (make demo)"

approve:
	@echo "not yet built — phase 8 (make approve)"

verify-audit:
	@echo "not yet built — phase 3 (make verify-audit)"

rollback:
	@echo "not yet built — phase 8 (make rollback)"

harvest:
	@echo "not yet built — phase 5 (make harvest)"

migrate:
	@echo "not yet built — phase TBD (make migrate)"
