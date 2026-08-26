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

.PHONY: setup doctor lint test clean data seal verify-seal eval demo approve verify-audit verify-audit-tamper rollback harvest migrate db-check config-check serve tunnel replay-webhooks

setup:
	$(PYTHON311) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

doctor:
	$(PY) -m src.config doctor

lint:
	$(PY) -m ruff check src tests scripts

test:
	$(PY) -m pytest -q

clean:
	rm -rf .venv .pytest_cache .ruff_cache second_rail.db second_rail.db-wal second_rail.db-shm
	rm -f cache/*.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

data:
	$(PY) -m data.generator

seal:
	$(PY) -m scripts.seal seal

verify-seal:
	$(PY) -m scripts.seal verify

eval:
	@echo "not yet built — phase 7 (make eval)"

demo:
	@echo "not yet built — phase 8/9 (make demo)"

approve:
	@echo "not yet built — phase 8 (make approve)"

verify-audit:
	$(PY) -m src.audit.verify --all

# evidence/audit_sample.jsonl (a small, committed excerpt of a real audit
# chain) is added in Phase 18 — evidence/audit/*.jsonl itself is per-run
# working output and stays untracked except for .gitkeep.
verify-audit-tamper:
	$(PY) -m src.audit.verify --tamper-test

rollback:
	@echo "not yet built — phase 8 (make rollback)"

harvest:
	$(PY) -m scripts.harvest_errors

migrate:
	$(PY) -m src.db.migrate

serve:
	$(PY) -m uvicorn src.ingest.app:app --port 8000

# cloudflared prints a public HTTPS URL on stdout — paste that URL, with
# /webhooks/razorpay appended, into the Razorpay dashboard's webhook config.
# The URL changes on every restart of this command, so the dashboard config
# goes stale each time — see LIMITATIONS.md.
tunnel:
	cloudflared tunnel --url http://localhost:8000

replay-webhooks:
	$(PY) -m scripts.replay_webhooks

db-check:
	$(PY) -m src.db.migrate --check

config-check:
	$(PY) -m scripts.config_check
