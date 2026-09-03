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

.PHONY: setup doctor lint test test-live clean data seal verify-seal eval demo approve demo-states verify-audit verify-audit-tamper rollback harvest migrate db-check config-check serve webui tunnel replay-webhooks gate-run failure-demo failure-demo-backup guardrail-proof classify choose-run watch thresholds judge-check secrets-audit dep-audit clean-clone-test scrub-cache

setup:
	$(PYTHON311) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

doctor:
	$(PY) -m src.config doctor

lint:
	$(PY) -m ruff check src tests scripts experiments

# Default suite a judge runs on a clean clone: hermetic, no network, no key,
# excludes only @pytest.mark.live (tests/test_executor.py's real Payment
# Link round-trip and any other test that needs RAZORPAY_KEY_ID/SECRET).
# Coverage for src/gate, src/execute, src/audit is printed explicitly below
# the main run — the three packages CLAUDE.md's non-negotiables bind
# hardest (no LLM, the idempotency boundary, the hash chain).
test:
	$(PY) -m pytest -m "not live" --cov=src --cov-report=term-missing -q
	@echo ""
	@echo "Coverage -- the three packages CLAUDE.md's non-negotiables bind hardest:"
	@$(PY) -m coverage report --include="src/gate/*"    | tail -1 | sed 's/^TOTAL/gate:   /'
	@$(PY) -m coverage report --include="src/execute/*" | tail -1 | sed 's/^TOTAL/execute:/'
	@$(PY) -m coverage report --include="src/audit/*"   | tail -1 | sed 's/^TOTAL/audit:  /'

# The one test in the suite that needs real Razorpay test-mode credentials
# and touches the network (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env) —
# never part of `make test`, run explicitly and separately.
test-live:
	$(PY) -m pytest -m live -q

clean:
	rm -rf .venv .pytest_cache .ruff_cache .coverage second_rail.db second_rail.db-wal second_rail.db-shm
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

# With no ID=, prints the pending/expired demo/approval_queue.json table.
# ID=ep_017 resolves that one item (approve by default; REJECT=1 to refuse,
# REASON="..." to record why). The interactive keypress prompt inside
# `make demo` resolves most human_keystroke episodes on the spot; this is
# the non-interactive fallback for whatever's left — see src/ui/approve.py.
approve:
	$(PY) -m src.ui.approve $(if $(ID),--id $(ID)) $(if $(REJECT),--reject) \
		$(if $(REASON),--reason "$(REASON)")

# Drives each of the 8 required demo states deliberately and exports one
# SVG per state into demo/states/ (rich.console.Console(record=True) +
# export_svg) — rehearsal material and proof every state exists, not just
# claimed. See scripts/demo_states.py.
demo-states:
	$(PY) -m scripts.demo_states

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

# --no-server-header drops the "Server: uvicorn" response header (uvicorn
# only appends it when the app's own response has none, so the flag is the
# whole fix -- see src/ingest/app.py's module docstring). This endpoint is
# meant to be reachable only through `make tunnel`'s cloudflared quick
# tunnel during a demo, never bound to a public interface directly.
serve:
	$(PY) -m uvicorn src.ingest.app:app --port 8000 --no-server-header

# Read-only companion dashboard (src/webui/) — a second window into the
# same run, not a replacement for `make demo`'s terminal. Binds to
# 127.0.0.1 only, on a different port than `make serve`'s public webhook
# receiver, on purpose: this process has a write action (approve/reject)
# and must never be reachable through the same tunnel as the webhook
# endpoint. Run it alongside `make demo` in another terminal.
webui:
	$(PY) -m uvicorn src.webui.app:app --port 8001 --host 127.0.0.1

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

# The eighteen Judge-Gap Matrix rows, one PASS/FAIL line each, exit 1 if any
# fail. Runs `make eval`, `make demo`, `make verify-audit`, `make rollback`
# and `make config-check` as real subprocesses (JG-05) rather than asserting
# they would work, so this takes a minute or two. SKIP_SLOW=1 drops the
# shell-out rows for fast iteration on the prose rows -- it is not a gate,
# and the tool says so in its own output when you use it.
judge-check:
	$(PY) -m scripts.judge_check $(if $(SKIP_SLOW),--skip-slow)

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

# Proves no secret ever entered git history or the working tree: greps
# `git log -p --all` and the tracked tree for key-shaped strings across
# every provider named in CLAUDE.md, and checks .env is gitignored and was
# never committed. Exits 1 with the offending commit/file on any finding —
# see scripts/secrets_audit.sh's own comment on why that stops the script
# rather than trying to fix anything.
secrets-audit:
	bash scripts/secrets_audit.sh

# Re-checks cache/*.json (the committed LLM response cache) for API keys,
# absolute paths from this machine, or real identifiers before it's staged.
scrub-cache:
	$(PY) scripts/scrub_cache.py

# pip-audit against the pinned requirement set. Any advisory that can't be
# fixed by a version bump must be logged in LIMITATIONS.md with the reason,
# not silently ignored -- the two --ignore-vuln entries below are exactly
# the two documented there ("make dep-audit (pip-audit): two advisories left
# unfixed on purpose"): click's fix breaks typer's argument parsing, and
# starlette's fix needs a fastapi jump too large to requalify before D9.
# A NEW advisory on any other package still fails this target.
dep-audit:
	$(PY) -m pip_audit -r requirements.txt -r requirements-dev.txt \
		--ignore-vuln PYSEC-2026-2132 \
		--ignore-vuln PYSEC-2026-161 --ignore-vuln PYSEC-2026-249 \
		--ignore-vuln PYSEC-2026-248 --ignore-vuln PYSEC-2026-1942 \
		--ignore-vuln PYSEC-2026-1941 --ignore-vuln PYSEC-2026-2281 \
		--ignore-vuln PYSEC-2026-2280

# Clones this repo (from the local .git dir, so it works offline) into a
# fresh temp dir, builds a venv from scratch, and runs the judge's own
# path (`make setup && make eval && make verify-audit && make judge-check`)
# with HOME redirected and every RAZORPAY_*/LLM_API_KEY var explicitly
# unset -- so it cannot accidentally succeed by reusing this machine's
# credentials or venv. Fails if the whole thing takes over 5 minutes or if
# the regenerated evidence/report.md disagrees with the committed one on
# any metric that's supposed to be deterministic.
clean-clone-test:
	bash scripts/clean_clone_test.sh
