# CLAUDE.md

Operating context for **Second Rail** — a Razorpay Buildathon submission. Read this before touching code. Full detail lives in `second-rail-build-blueprint.md` at repo root; this file is the fast-reference layer on top of it. When something here and the blueprint disagree, the blueprint wins — update this file.

## What this project is

Second Rail is the automated recovery desk for Razorpay payments that fail *after* the customer's session ends — the ~third of failed transactions nobody re-attempts. It ingests `payment.failed`, runs a deterministic eligibility gate, diagnoses the cause from the issuer's own error strings, picks one action from a pre-registered admissible set, gets a human keystroke above a rupee threshold, executes it as a **cancellable Razorpay Payment Link** the customer authenticates themselves, and reports what came back — net of the cost of being wrong — against a sealed 200-episode batch.

It is not "an AI agent that recovers revenue." It's a diagnosis-to-bounded-action loop plus honest accounting. **Positioning discipline:** never claim "nobody owns the payment after the session ends" — Razorpay already ships payment links and abandoned-checkout recovery. The seam this project owns is *automated per-episode diagnosis driving a bounded, gated, audited action*, not the category.

## Schedule

| Date | Milestone |
|---|---|
| D1 — 25 Aug 2026 | Build starts |
| D9 — 2 Sep | Freeze |
| D11 — 4 Sep | Submit |
| 5 Sep | Close |

## Non-negotiables — violating any of these breaks a hard gate

These map to `[F][HARD]` / `[W][HARD]` judge clauses. Don't refactor around them, and flag rather than silently work around any conflict with these.

- **No code path moves money.** The only external effect the system produces is a *cancellable* Payment Link with `expire_by` set, requiring the customer's own authentication. State this as a README heading.
- **Every money-adjacent threshold lives in config, not code** — `config/guardrails.yaml`, `config/policy_table.yaml`, `config/taxonomy.yaml`. `grep -rn '5000\|₹' src/` must return nothing.
- **The LLM never touches `src/gate/`, `src/execute/`, `src/attribute/`, or `src/audit/`.** It only classifies (diagnose) and selects from an already-constrained set (choose) — 2 calls/episode max, both content-hash cached. Enforced by `test_llm_boundary.py`, which greps those packages for the client symbol and fails if found.
- **Idempotency key = `sha256(payment_id + ':' + policy_rule_id)[:32]`** — never the webhook event id. Used as both the Payment Link `reference_id` and a SQLite `UNIQUE` constraint.
- **Default mode is `--dry-run`.** `--execute` is required and explicit for any real Razorpay call. Assert this with a test that runs the CLI with no flag and checks zero HTTP POSTs.
- **No episode is ever silently dropped.** `count(episodes) == count(actioned) + count(suppressed) + count(execution_failed)`, asserted in eval.
- **No real PII, ever.** Synthetic data only, seeded generator committed (`data/generator.py`). No card PAN stored, logged, or rendered anywhere.
- **No secrets in git history.** `.env.example` only. Verify with `git log -p | grep -E 'rzp_(live|test)_'` → nothing.
- **`outcome_model.md` is pre-registered.** Committed on D1, before `src/attribute/` exists, with a git timestamp strictly earlier than the first eval-run commit. Never amended — corrections go in an appendix with their own commit.
- **The recovery figure is never a bare point estimate.** Range only, everywhere it appears, framed as a design target under stated assumptions — and it comes *third* in the report, after non-circular metrics (guardrail correctness, admissibility rate, throughput/cost) and externally-anchored ones (harvested error strings, classifier vs. regex).
- **At least one reported result must shrink the builder's own claim** (e.g. regex beating the LLM classifier on common error families) — reported as a headline, not a footnote.

## Module boundaries

```
src/ingest/     webhook receipt, signature verify, dedup, normalization      [no LLM]
src/gate/       eligibility checks, caps, quiet hours, frequency             [no LLM]
src/diagnose/   regex baseline + LLM classifier + confidence + rationale     [LLM]
src/choose/     policy engine → admissible set; LLM selects 1 of ≤3          [LLM, constrained]
src/execute/    idempotency, retry/backoff, Payment Links, rollback          [no LLM]
src/attribute/  outcome listener, attribution window, ledger                 [no LLM]
src/audit/      hash chain, append, verify                                   [no LLM]
```

One episode, end to end: webhook → ingest (dedup on `payment_id`) → gate (7 ordered checks, each audited) → diagnose (regex first; unmatched tail → LLM) → choose (policy table constrains, LLM picks 1 of ≤3) → gate again (post-selection re-check of caps/DND/quiet hours/idempotency) → approval queue if human tier → execute (idempotent Payment Link, backoff on 429/5xx) → persistence + hash-chained audit → outcome listener → attribute → response surface (Rich live stream + `evidence/report.md`).

**This is the pipeline's logical shape, not today's wiring.** `src/ingest/` stops at dedup + normalization by design (see that package's own module docstring on the 50ms endpoint target) — gate through attribute run as a batch pass over already-ingested episodes (`make eval`, `make demo`), never inline off the same webhook request. See `docs/out-of-scope.md`'s "Real-time webhook-to-pipeline processing" entry before describing this as a live reaction to an incoming webhook, on camera or in writing.

## Stack

Python 3.11 · FastAPI + uvicorn (webhook receiver) · Pydantic 2.9 (schema validation at every boundary) · SQLite stdlib, WAL mode (persistence + idempotency via `UNIQUE`) · append-only JSONL, hash-chained (audit) · Typer (CLI) · Rich `Live`/`Table`/`Progress` (this is the demo surface) · razorpay SDK for orders/payments/customers, raw `httpx` for Payment Links so request/response land in the audit record verbatim · hand-rolled backoff (~40 lines, not `tenacity` — attempt/delay must be explicit in the audit record) · PyYAML for config (comments carry threshold justifications) · Groq (`openai/gpt-oss-20b`) primary for evidence generation / Gemini 3.6 Flash and `gpt-4o-mini` as configured alternatives behind the same interface — `gemini-2.5-flash` returned HTTP 404 "no longer available to new users" on a live call 2026-08-30, and Gemini 3.6 Flash's free tier could not sustain this phase's evidence pass at any RPM pacing that actually held (see BUILD_LOG.md) — content-addressed disk cache · matplotlib → committed PNGs · cloudflared quick tunnel · pytest.

No queue, no auth provider, no messaging provider, no cloud deploy, no Docker, no CI/CD — all stated as deliberate choices in `LIMITATIONS.md`, not omissions.

## Commands

```
make setup              # pinned venv install
make eval                # offline: fixtures + committed LLM cache, no network, no key — must finish <5 min on a clean machine
make demo                # Rich live stream; --execute required for real Razorpay calls, default dry-run
make approve              # approval queue for human-tier episodes
make rollback RUN_ID=X   # cancels every link a run created, prints a per-link result table
make verify-audit        # walks the hash chain, must print "chain intact - N records" in <2s (ASCII hyphen, not an em-dash — see KNOWN_ISSUES.md Issue 2)
make harvest              # one-time D2 job: forces real Razorpay test-mode failures into evidence/harvested_errors.jsonl
```

Submission gate: `make eval && make demo --dry-run && make verify-audit && make rollback` must all exit 0 — nothing half-wired, nothing stubbed.

## Data model

SQLite, ISO-8601 timestamps with `+05:30` offset. Key tables: `customer`, `episode` (UNIQUE `payment_id` — the dedup boundary), `harvested_error` (the non-circular anchor, also committed as JSONL), `taxonomy_class`, `gate_check`, `diagnosis`, `policy_rule` (UNIQUE `(cause_class, amount_band, segment, instrument)` — guarantees deterministic mapping), `decision`, `approval`, `execution` (UNIQUE `idempotency_key` — the single most load-bearing constraint in the schema), `webhook_event`, `attribution`, `ledger_entry`, `run`, `exception_entry`, `audit_record`. Full column-level spec is in blueprint §6 — check there before adding or renaming a column.

## Open decisions — do not assume, ask

Unresolved conflicts between the proposal and judge-expectations source docs. If work touches one of these, stop and ask rather than picking silently:

| # | Question | Blocks |
|---|---|---|
| U-04 | Taxonomy frozen D1 (proposal) vs. built from harvested strings (judge, needs keys) — provisional-then-ratify plan proposed | `config/taxonomy.yaml` |
| U-05 | Sealed split needs a genuine distribution shift, or just a rename to "sealed split"? | `holdout/` design, recovery number |
| U-07 | Which baseline survives: do-nothing, fixed-retry-at-T+30, or both? | `src/attribute`, report baselines section |
| U-08 | Attribution window length is undefined in both source docs (24h/48h/72h?) | `outcome_model.md`, recovery figure |
| U-11 | LLM call budget contradiction: "~1/episode" cost assumption vs. 2 sequential calls required by classify→constrain→select | Cost model, `src/diagnose`, `src/choose` |
| U-14 | Exact hard-refusal band beyond issuer-outage cluster >15 / opted-out / already-paid / age >72h | `config/guardrails.yaml`, escalation tiers |

Full list (U-01 through U-15) is in blueprint §10. U-02, U-03, U-08, U-13 were flagged as needed before D1 hour 3 — confirm those are resolved before starting build work.

## Priority discipline

P0 items in the blueprint's requirements ledger (§2) are what the demo or submission is broken without — build those first. Already-decided cuts: web approval UI → JSON queue + `make approve`; Hinglish copy → English only; sensitivity sweep → 3 parameters, not all.

## Voice and evidence discipline

- README written from scratch, first person, no drafting artifacts (strip any `<cite>` tags) — the panel specifically checks whether README, `BUILD_LOG.md`, and video sound like one person.
- `BUILD_LOG.md` gets one honest entry per working session starting D1, including at least one place the first hypothesis was wrong. Write it as you go — it can't be reconstructed retroactively.
- Every illustrative arithmetic figure is labeled "illustrative." The 33%-never-re-attempted figure is always attributed to Razorpay as their own claim; the "30% of revenue" figure is never used.
