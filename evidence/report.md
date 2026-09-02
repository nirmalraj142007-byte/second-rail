# Second Rail — Results
Run `01M1HFCGP0EVKTQVFARXX580V8` · `c7ff53b` · config `9e22c9aa673f…` · 2026-09-02T22:01:56+05:30
Sealed split: sha256 verified — 200 episodes (see `holdout/SEAL.sha256`) · shift: BANK_E is reserved for the sealed split only.

Attribution rule AR-01, window 48h.

## 1. What I measured

Nothing in this section depends on the outcome model in `outcome_model.md` — every number below is either read off a real API response or counted directly from what the batch actually did.

### Guardrail correctness under fault injection

Read from `evidence/guardrail_proof.json` — the real N=108 run (live).
N is capped at 108 by the number of gate-eligible episodes in `data/train.jsonl`; this is the full available set, not a partial sample.
This tool's own consecutive-executor-error tolerance was raised to 5 for this run (from the shared production default of 3 in `config/guardrails.yaml`, which stays unchanged) — sustained real-API calls at volume produce sporadic Razorpay-side rate-limiting that isn't a systemic failure signal for a tool whose whole job is deliberately hammering the real API; see BUILD_LOG.md.
Stopping rule `consecutive_executor_errors` fired this run — only 10/108 requested episodes were actually reached; every metric below is real, just over a smaller N than requested.
**Real + fixture, stated plainly, not rounded up:** 10 real, live-verified test-mode Payment Link creation(s) back this proof — the other 98 requested episodes were never attempted in this run at all, live or fixture (this tool has no mid-run real-to-fixture handoff). Separately, and independently of this proof, `make eval`'s sealed-split evaluation exercises the full 200-episode batch through `FixtureExecutor` — synthesized responses, not real captured ones (no fixture file exists per episode), and not re-verified against the live API. The two are complementary, not the same measurement: this table is real-API-verified correctness at a real, if small, N=10; the sealed-split figures elsewhere in this report are full-batch behavioural coverage with no live network calls.

| metric | value | requirement |
|---|---|---|
| duplicate links created | 0 | must be 0 |
| cap breaches | 0 | must be 0 |
| quiet-hour contacts | 0 | must be 0 |
| idempotency collisions correctly detected | 10/10 | — |
| links created and cancelled | 5 | every link this proof created |

5 link(s) on the real Razorpay API carry notes.run_id='01M1HEV5580ZXWYFJ11DJA50EQ', vs 5 distinct idempotency key(s) recorded locally

### Action admissibility rate

100.0% of agent choices fell inside the pre-registered admissible set (n=108). By construction this can only ever be 100% or the run will already have halted — `ActionSelector.select()` raises `AdmissibilityError` rather than ever returning a choice outside the set.

### Stopping rules

Stopping rule fired this run: `cap_breach` — the batch was 200 episodes, 131 were processed before the rule halted the run, 69 were never reached. Not silently dropped — every unreached episode is still counted (`pending`) in the accounting invariant `src/runner.py` asserts on every run. This is a real guardrail firing against real sealed data, not a staged demo — see `make guardrail-proof` for the dedicated, controlled fault-injection version of this same class of proof.

### Throughput and LLM cost

Throughput: 1310.0 episodes/min over 131 of 200 sealed episodes processed.

LLM cost this run (cache-aware, 0 paise on every cache hit): Rs 0.00 (measured), Rs 0.00 (measured) per 100 episodes.

Real cost (ignores cache, recomputed from actual token counts on every call — what this run would have cost with an empty cache): Rs 1.09 (measured), Rs 0.55 (measured) per 100 episodes.

Model `openai/gpt-oss-20b`, prompt version(s) classify_v1, select_v1. 107 diagnose call(s) resolved by regex, free — never touched the cache or the model at all. Of the 109 call(s) that did need the model (LLM-resolved diagnoses plus every choose call), cache hit rate: 100.0% (109/109).

## 2. What was externally anchored

Inputs I did not author: real error strings forced out of Razorpay's own test-mode failure simulation (20 records, captured 2026-08-26), and Razorpay's own published error-code documentation (evidence/razorpay_error_codes_snapshot.md).

| source | n | regex accuracy | LLM accuracy |
|---|---|---|---|
| harvested strings (raw, real fields) | 20 | 5.0% | 20.0% |
| Razorpay doc snapshot (independent label source) | 17 | 82.3% | 88.2% |

## 3. Where the claim gets weakest

On harvested strings (raw, real fields) (n=20) — the hardest, most externally-anchored data anywhere in this evaluation — classifier accuracy collapses to 20.0%. The LLM still beats the regex baseline there (5.0%), but that comparison is not the finding worth taking seriously here — both are weak. The humbling number is the 20.0% itself, not which method produced it.

Separately (self-generated data, not externally anchored — see the top-5 error-family table below): regex and the LLM tied across the top 5 error families by volume. (computed on the train split via `make classify` — a general classifier finding, not scoped to this sealed batch.)

| error family | volume | regex accuracy | LLM accuracy |
|---|---|---|---|
| insufficient_fund | 88 | 100.0% | 100.0% (n=8) |
| gateway_technical_error | 69 | 100.0% | 100.0% (n=8) |
| authentication_failed | 67 | 100.0% | 100.0% (n=8) |
| card_declined | 50 | 100.0% | 100.0% (n=8) |
| card_number_invalid | 39 | 100.0% | 100.0% (n=8) |

## 4. Design target under stated assumptions

This is a simulator. Every figure below passes through the customer-response model in `outcome_model.md`; sections 1-3 above do not.

Sensitivity sweep, +/-30% on three parameters: response probability, attribution window, goodwill proxy (false-positive cost).

- **response probability**: outcome_model.md §2's per-class base rate and its segment/amount multipliers are all named assumptions, not measurements — the single most load-bearing unmeasured number in the recovery figure.
- **attribution window**: outcome_model.md §3 states 48h was chosen by reasoning from the 72h action-age cap, not by fitting a real payment_link.paid latency distribution — no such distribution existed to fit.
- **goodwill proxy (false-positive cost)**: outcome_model.md §4 states the SMS cost is a quoted real price but the Rs 15 goodwill figure is an assumption — 'a named guess... no survey, support-ticket log, or churn data backs this number.'

Note on the attribution-window sweep: it is included above for disclosure completeness (one of the three pre-registered parameters) but is a structural no-op on the figures below — see src/report/sensitivity.py's module docstring for why the expected-value method used here has no elapsed-time signal for the window to act on.

### Second Rail

**Second Rail** — 99/108 gate-eligible episodes contacted, out of the 200-episode sealed batch.
Stopping rule `cap_breach` fired partway through this run — see section 1.

gross Rs 51,497 - Rs 95,595 | false-positive cost Rs 11 - Rs 20 (1 contact(s)) | **NET Rs 51,482 - Rs 95,580**

### FIXED_RETRY_AT_T30 baseline

**FIXED_RETRY_AT_T30 baseline (Runner's gate-only fallback: every gate-eligible episode gets `placeholder_action`/`P-00`, unconditionally)** — 102/102 gate-eligible episodes contacted, out of the 200-episode sealed batch.
Stopping rule `cap_breach` fired partway through this run — see section 1.

gross Rs 51,427 - Rs 95,464 | false-positive cost Rs 11 - Rs 20 (1 contact(s)) | **NET Rs 51,412 - Rs 95,449**

Recovery is computed as an expected value — Sigma(response_probability x amount_paise) over episodes this run actually contacted — using the sealed split's per-episode response_probability field (outcome_model.md's formula, assigned once per episode), not a resampled boolean draw. This number passes through an outcome model I wrote; sections 1-3 do not.

This sweep perturbs my own parameters and widens a band around a quantity I invented. It is disclosure, not evidence. Sections 1-3 are the evidence.

## 5. Exceptions

32 episode(s) excluded from the recovery figures this run, by reason_code — no episode is ever silently dropped, see the accounting invariant in `src/runner.py`.

| reason_code | count |
|---|---|
| `quiet_hours_block` | 22 |
| `no_action_selected` | 9 |
| `exposure_ceiling_exceeded` | 1 |

`execution_failed` episodes are **excluded** from the recovery figures in section 4 — 0 this run.

### Worked examples

- `pay_synthetic_00403` (card, Rs 106.66 (illustrative), error_reason='pin_not_set') — **quiet_hours_block**: quiet_hours check failed
- `pay_synthetic_00411` (netbanking, Rs 2,105.27 (illustrative), error_reason='upi_app_not_available') — **quiet_hours_block**: quiet_hours check failed
- `pay_synthetic_00413` (upi, Rs 972.18 (illustrative), error_reason='authentication_failed') — **quiet_hours_block**: quiet_hours check failed

## 6. Classifier detail

Self-graded against generator truth, n=200. Accuracy: 100.0%.

| class | precision | recall | f1 | support |
|---|---|---|---|---|
| C1 | 1.000 | 1.000 | 1.000 | 46 |
| C2 | 1.000 | 1.000 | 1.000 | 31 |
| C3 | 1.000 | 1.000 | 1.000 | 17 |
| C4 | 1.000 | 1.000 | 1.000 | 29 |
| C5 | 1.000 | 1.000 | 1.000 | 12 |
| C6 | 1.000 | 1.000 | 1.000 | 20 |
| C7 | 1.000 | 1.000 | 1.000 | 15 |
| C8 | 1.000 | 1.000 | 1.000 | 18 |
| C9 | 1.000 | 1.000 | 1.000 | 12 |

### Confusion matrix (rows = true class, columns = predicted)

| true \\ pred | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | C9 |
|---|---|---|---|---|---|---|---|---|---|
| C1 | 46 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| C2 | 0 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| C3 | 0 | 0 | 17 | 0 | 0 | 0 | 0 | 0 | 0 |
| C4 | 0 | 0 | 0 | 29 | 0 | 0 | 0 | 0 | 0 |
| C5 | 0 | 0 | 0 | 0 | 12 | 0 | 0 | 0 | 0 |
| C6 | 0 | 0 | 0 | 0 | 0 | 20 | 0 | 0 | 0 |
| C7 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 0 |
| C8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 0 |
| C9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 12 |

### Cost of confusion (rupees) — for each confusion, the deterministic fallback_priority-first admissible action under the true class vs. the predicted class, valued at response_probability x amount for the episodes actually confused this way — not a live LLM re-selection, disclosed as a simplification

| true class | predicted class | count | mean delta (illustrative) |
|---|---|---|---|

## 7. What this does not measure

- Real customer behaviour — every response is a simulated draw from outcome_model.md's formula, not an actual person deciding whether to pay.
- Generalisation beyond the seeded distribution shift — BANK_E and the 11 reserved harvested error strings are the only shift this split carries; a real issuer's traffic could differ in ways this generator never modelled.
- Anything at production volume — 200 episodes in one batch, not a sustained 10k/episode-a-day load; see LIMITATIONS.md for the three places this design is known to break first.
- Partial payments — a customer paying less than the link amount is recorded not_recovered by AR-01, even though the merchant did receive some money.
- Recoveries through channels this run did not create — a payment that later shows as paid through a different link or a different channel entirely is never claimed as this system's recovery.

