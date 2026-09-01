SHELL := /bin/bash
PYTHON311 := python3.11
N ?= 200

ifeq ($(OS),Windows_NT)
	VENV_BIN := .venv/Scripts
	PY := $(VENV_BIN)/python.exe
	PIP := $(VENV_BIN)/pip.exe
else
	VENV_BIN := .venv/bin
	PY := $(VENV_BIN)/python
	PIP := $(VENV_BIN)/pip
endif

.PHONY: setup doctor lint test clean data seal verify-seal eval demo approve verify-audit verify-audit-tamper rollback harvest migrate db-check config-check serve tunnel replay-webhooks gate-run failure-demo failure-demo-backup guardrail-proof classify choose-run watch thresholds

setup:
	$(PYTHON311) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

doctor:
	$(PY) -m src.config doctor

lint:
	$(PY) -m ruff check src tests scripts experiments

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

# LIVE=1 swaps FixtureExecutor for RazorpayExecutor (real, still test-mode,
# Payment Link creation). Default (no LIVE) is fixture mode: no network for
# the executor, though the diagnose/choose LLM calls still hit the real
# provider unless every prompt this run produces is already cached — see
# scripts/eval.py's module docstring.
eval:
	$(PY) -m scripts.eval $(if $(LIVE),--live)

# Sources default to data/train.jsonl + holdout/sealed.jsonl (600 episodes
# combined) inside src/runner.py — see that file's module docstring for why
# train.jsonl alone (400 episodes) can't satisfy this phase's own "process
# all 600" acceptance test. Zero LLM calls, zero network calls.
gate-run:
	$(PY) -m src.runner --gate-only

# EXECUTE=1 turns on real Razorpay calls; LIMIT=N caps the episode count.
# Both are bare make-variable values (not flags) — translated into the
# script's --execute / --limit flags here so `make demo` alone stays a
# plain, flagless dry-run.
demo:
	$(PY) -m scripts.demo $(if $(EXECUTE),--execute) $(if $(LIMIT),--limit $(LIMIT))

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
	$(PY) -m src.execute.rollback --run-id $(RUN_ID)

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

# Phase 14 — sweeps auto_approve_ceiling_paise, outage_cluster_threshold,
# and executor_retry_cap over real code (GateEngine / compute_cluster_
# membership / RazorpayExecutor), never a simulated result. Writes
# experiments/thresholds/{auto_approve,outage_cluster,retry_cap}.md +
# charts/*.png + results_*.json. No LLM calls, no network, no key
# required — runs in well under a minute.
thresholds:
	$(PY) -m experiments.thresholds.run_auto_approve
	$(PY) -m experiments.thresholds.run_outage_cluster
	$(PY) -m experiments.thresholds.run_retry_cap

# SPLIT=train|sealed (required). Regex-vs-LLM head-to-head, coverage, cost,
# self-graded + externally-anchored classification metrics. Writes
# evidence/classification_metrics.json. Cached LLM responses make a second
# run of the same split free of both LLM calls and network access.
classify:
	$(PY) -m scripts.classify --split $(SPLIT)

# SPLIT=train|sealed (required). Real diagnose-then-choose cascade over
# every episode: admissibility rate, chosen-action distribution, escalation
# tier distribution, and the count of episodes where the model named a
# feature outside LLM_VISIBLE_FEATURES. Writes evidence/choose_metrics.json.
# Cached LLM responses make a second run of the same split free of both LLM
# calls and network access.
choose-run:
	$(PY) -m scripts.choose_run --split $(SPLIT)

# Primary failure demo (video beat 2:20-2:42) — real Razorpay test-mode
# calls, a 12-episode slice, 429 injected at episode 7. Needs
# RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env.
failure-demo:
	$(PY) -m scripts.failure_demo

# Backup failure demo — no network, no keys. Duplicate payment.failed
# webhook replay, dedup no-op.
failure-demo-backup:
	$(PY) -m scripts.failure_demo_backup

# N real Payment Link creations under fault injection — the headline
# non-circular metric. `DRY_RUN_FIRST=1` validates the plan end to end
# with FixtureExecutor first, at zero API cost (equivalent to passing
# --dry-run-first directly to the script — `make`'s own argument parser
# cannot take a bare `--flag` after this target's name, so that flag must
# go through this variable, or call `python -m scripts.guardrail_proof
# --n N --dry-run-first` directly). TOLERANCE=n overrides the tool's own
# consecutive-executor-error stopping threshold (default 5, scoped to
# this tool only — config/guardrails.yaml's shared default of 3 is never
# touched); see BUILD_LOG.md for why this tool specifically tolerates
# more sporadic real-API failures than a production run does.
guardrail-proof:
	$(PY) -m scripts.guardrail_proof --n $(N) $(if $(DRY_RUN_FIRST),--dry-run-first) \
		$(if $(TOLERANCE),--consecutive-error-tolerance $(TOLERANCE))

# RUN_ID=x uses webhooks (default); add POLL=1 to use polling instead — the
# demo's insurance policy when the cloudflared tunnel or webhook server is
# down. Prints which mode is active as the first line, then every
# attribution this pass resolved, then gross/fp/net.
watch:
	$(PY) -m scripts.watch --run-id $(RUN_ID) $(if $(POLL),--poll)
