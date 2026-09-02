# Second Rail

The recovery desk for Razorpay payments that fail *after* the customer's session has ended. It ingests `payment.failed`, diagnoses the cause from the issuer's own error string, picks one action from a pre-registered admissible set, takes a human keystroke above a rupee threshold, executes it as a cancellable Payment Link, and reports what came back — net of the cost of being wrong — across a sealed 200-episode batch.

## Quickstart

```bash
make setup          # pinned venv, no API key needed
make eval           # sealed-split evaluation -> evidence/report.md, ~20s, no key, no network
make verify-audit   # walks the hash chain, prints "chain intact - N records"
```

## No code path in Second Rail moves money

This is the design's spine, so it is a heading and not a sentence buried in a paragraph.

The only external effect this system can produce is a **cancellable Razorpay Payment Link** with `expire_by` set, which the customer authenticates themselves. There is no debit, no refund, no payout, no transfer, no auto-capture — not gated behind a flag, not implemented and disabled. Those API calls do not exist in `src/`. `make rollback RUN_ID=x` cancels every link a run created and prints a per-link result table. Default mode is `--dry-run`; real Razorpay calls require an explicit `--execute`.

## What it does, and what it does not claim

One episode, end to end: webhook -> ingest (dedup on `payment_id`) -> gate (7 ordered checks, each audited) -> diagnose (regex first, unmatched tail to an LLM) -> choose (a policy table constrains the set, the model picks 1 of at most 3) -> gate again -> approval queue if the episode is above the auto-approve ceiling -> execute (idempotent Payment Link) -> hash-chained audit -> outcome listener -> attribute -> report.

**On positioning.** Razorpay already ships payment links, Optimizer, Intelligent Payment Retry and abandoned-checkout recovery, and merchants run their own retry emails. I am not claiming an empty category. The seam this project owns is narrower: **automated per-episode diagnosis driving a bounded, gated, audited action** for failures that land after the session is gone. Razorpay's own Optimizer launch material states that nearly 33% of failed transactions are never re-attempted — that is their figure and their claim, cited as such, not a number I measured.

## Results

Full detail, including the confusion matrix and the exception list, is in **[evidence/report.md](evidence/report.md)**, which is committed — you can read every result without running anything.

The ordering below is deliberate. The numbers that did not pass through a model I wrote come first.

### 1. Non-circular — measured against real API responses and real counts

| metric | value |
|---|---|
| duplicate links created, real Razorpay test-mode API | **0** |
| cap breaches | **0** |
| quiet-hour contacts | **0** |
| idempotency collisions correctly detected | **10/10** |
| action admissibility rate | **100%** (n=108) |
| throughput | **1,167 episodes/min** |
| LLM cost per 100 episodes, cache ignored | **Rs 0.55** (measured from real token counts) |

The guardrail proof reached 10 of 108 requested episodes before its consecutive-executor-error stopping rule fired against real Razorpay-side rate limiting. That is a small N and the report says so in those words rather than rounding it up.

### 2. Externally anchored — labels I did not author

Real `error_code` / `error_reason` strings forced out of Razorpay's own test-mode failure simulation, plus Razorpay's published error-code documentation as an independent label source.

| source | n | regex | LLM |
|---|---|---|---|
| harvested strings (raw, real fields) | 20 | 5.0% | 20.0% |
| Razorpay doc snapshot | 17 | 82.3% | 88.2% |

### 3. The result that costs me

On the harvested strings — the hardest and most externally-anchored data in this evaluation — **classifier accuracy collapses to 20.0%**. The LLM beats the regex baseline there (5.0%), but that is not the finding worth taking seriously: both are weak, and the humbling number is the 20.0%, not which method produced it. Separately, on the top five error families by volume, **free regex ties the paid model at 100% on both**. The model earns its cost only on the unmatched tail.

### 4. Recovery — a design target, not a measurement

**NET Rs 51,482 - Rs 95,580** across the 200-episode sealed split, against a fixed-retry baseline of **Rs 51,412 - Rs 95,449**, net of false-positive cost, as a range under a +/-30% sweep of three pre-registered parameters.

Read that number sceptically. It passes through a customer-response model I wrote myself, pre-registered in [outcome_model.md](outcome_model.md) before any eval ran (`git log` will confirm the timestamps). The sweep perturbs my own parameters and widens a band around a quantity I invented. It is disclosure, not evidence. Sections 1-3 are the evidence.

## Where the LLM is and is not

The model does exactly two things per episode, both content-hash cached: classify a cause when the regex baseline does not match, and pick one action from an already-narrowed set of at most three. Everywhere else it is refused outright.

It never computes an amount, evaluates a cap, decides eligibility or quiet hours, determines attribution, produces any reported metric, or chooses an action outside the policy table's admissible set. `src/gate/`, `src/execute/`, `src/attribute/`, `src/audit/`, `src/ingest/` and `src/db/` are enforced LLM-free by `tests/test_llm_boundary.py`, which greps those packages for the client symbol and fails the build if it finds one.

The full list, with the enforcing test named against each refusal, is in **[docs/where-the-llm-is-not.md](docs/where-the-llm-is-not.md)**. It is the document I would read first if I were reviewing this repo.

## Guardrails

Every money-adjacent threshold lives in [config/guardrails.yaml](config/guardrails.yaml) — 16 lines, readable in under a minute, with a justification comment on every number. `grep -rn '5000' src/` returns nothing.

| bound | value |
|---|---|
| max recovery actions per payment | 1 |
| max contacts per customer | 2 per 7 days |
| quiet hours | 21:00-09:00 IST, hard block |
| max episode age for action | 72h |
| auto-approve ceiling | Rs 5,000 |
| batch contact ceiling before human gate | 50 |
| per-run exposure ceiling | Rs 2,00,000 notional |
| executor retry cap | 3, backoff 1s/2s/4s |
| halt on consecutive executor errors | 3 |
| default mode | `--dry-run` |

Escalation is tiered, not binary: `auto` below the ceiling, `human_keystroke` above it, `hard_refuse` in a third band (issuer-outage cluster, opted-out, already paid, older than 72h). Each tier is written to the audit log with a named reason, not just a tier label.

Three of these thresholds have an experiment behind them rather than a guess — auto-approve ceiling, outage-cluster threshold and retry cap — in [experiments/thresholds/](experiments/thresholds/). The rest carry an explicit "not experimentally derived" marker saying so, and `make config-check` fails if any number has neither.

Verify it yourself:

```bash
make config-check   # 9 checks over the config surface
make judge-check    # 18 checks over the whole submission
```

## Limitations

The honest ones, in full, are in [LIMITATIONS.md](LIMITATIONS.md), [docs/scaling-failures.md](docs/scaling-failures.md) and [docs/out-of-scope.md](docs/out-of-scope.md). The three that matter most:

- **The recovery figure is simulated.** No real customer decided whether to pay. The assumption file is committed and pre-registered; the three metric groups above it are not affected.
- **The data is synthetic.** DPDP Act 2023 — no real PII, ever. The seeded generator is committed at [data/generator.py](data/generator.py).
- **This design breaks at 10k episodes/day in three named places**, in the order I would fix them: the in-process webhook queue, the executor's missing per-issuer circuit breaker, and audit-chain partitioning by run. [docs/scaling-failures.md](docs/scaling-failures.md) has the symptom and the fix for each.

I am not a lawyer and none of this is legal advice. The compliance items named across these documents — DPDP Act 2023, TRAI DLT, RBI tokenisation, NPCI mandate retry rules — are flagged because they are load-bearing for what this project claims, not because this constitutes a compliance review. Anything shipped for real needs counsel.

## Architecture

```
payment.failed webhook
        |
        v
  [ ingest ]  signature verify, dedup on payment_id, out-of-order handling   NO LLM
        |
        v
  [ gate ]    7 ordered checks: duplicate, terminal_seen, opt_out,           NO LLM
        |     episode_age, amount_cap, frequency_cap, quiet_hours
        v
  [ diagnose ] regex baseline first; only the unmatched tail reaches         LLM (1 call max)
        |      the classifier. Confidence + rationale recorded.
        v
  [ choose ]  policy table maps (cause x amount band x segment x             LLM (1 call, constrained)
        |     instrument) -> admissible set of <= 3. Model picks one.
        v
  [ gate ]    post-selection re-check: caps, DND, quiet hours, idempotency   NO LLM
        |
        v
  [ approve ] auto | human keystroke | hard refuse                           NO LLM
        |
        v
  [ execute ] idempotency key = sha256(payment_id + ':' + policy_rule_id)    NO LLM
        |     -> Payment Link reference_id AND a SQLite UNIQUE constraint.
        |     Hand-rolled backoff so each attempt and delay is auditable.
        v
  [ attribute ] outcome listener -> 48h window -> ledger: gross, fp_cost,    NO LLM
        |       net. net is computed in exactly one function.
        v
  [ audit ]   append-only JSONL, every record hash-chained to the previous   NO LLM
```

Stack: Python 3.11, FastAPI, Pydantic 2.9, SQLite (WAL), Typer, Rich, matplotlib, raw `httpx` for Payment Links so the request and response land in the audit record verbatim. No queue, no auth provider, no cloud deploy, no Docker, no CI — each a deliberate cut, listed with its reason in [docs/out-of-scope.md](docs/out-of-scope.md).

## Build notes

[BUILD_LOG.md](BUILD_LOG.md) has one entry per working session from day one, including a `## Wrong turns` index at the top pointing at every place my first hypothesis was wrong. There are ten of them. The one I would lead with: I spent a session certain my dedup logic was broken, and it was the tunnel re-delivering on reconnect — which is why the idempotency key is derived from `payment_id` and never from the webhook event id.

[KNOWN_ISSUES.md](KNOWN_ISSUES.md) carries the defects I found and have not yet closed, rather than the ones I fixed quietly.

### Everything a judge might run

```bash
make setup && make eval && make verify-audit    # the three-line path, no key
make judge-check                                # 18 checks, exits 1 on any failure
make demo                                       # dry-run by default; EXECUTE=1 for real calls
make rollback RUN_ID=x                          # cancel every link a run created
make verify-audit-tamper                        # proves the chain detects tampering
make thresholds                                 # re-runs the three threshold experiments
make guardrail-proof N=200                      # real Payment Link creations under fault injection
```
