# Where the LLM is not

*Complete as of Phase 17, 2 Sep 2026. Every package named below now
exists, and every "where enforced" line points at code or a test I can run,
not at a plan. `tests/test_llm_boundary.py` is the mechanism referenced
throughout: it walks `src/gate/`, `src/execute/`, `src/attribute/`,
`src/audit/`, `src/ingest/` and `src/db/` and fails the build if any file in
them imports the LLM client module or contains the strings `"openai"`,
`"genai"`, or `"anthropic"`.*

I call an LLM twice per episode, at most: once to classify a cause when the
regex baseline doesn't match, once to pick one action from an
already-narrowed set of at most three. Both calls are content-hash cached,
so a re-run of the same episode costs nothing and returns the same answer.
That is the entire surface. Everywhere else in this system the model is
refused outright — not "discouraged," refused, with a test that greps the
package for the client symbol and fails the build if it's there
(`test_llm_boundary.py`).

This document is the list of exactly which decisions that refusal covers,
and why each one is a decision I don't trust a model to make. I wrote it
because the negative space is the part that shows judgement: everyone can
show you where they used a model.

A note on how to read the "where enforced" lines. I've tried to distinguish
three different strengths of guarantee, because they are not the same thing
and pretending they are would defeat the point of the document:

- **Structural** — the capability does not exist in the codebase at all
  (there is no payout call to gate).
- **Tested** — a named test fails if the boundary is crossed.
- **Conventional** — the code is written this way and nothing automated
  stops someone changing it. I say so where that's the case.

## Amount computation

**Decision refused:** how much money a Payment Link is created for, and
every arithmetic step that leads to that number.

**Why the model is kept out:** the amount is read directly off the
original `payment.failed` webhook's `amount` field and passed through
unchanged. There is no step where a model summarizes, rounds, estimates,
or otherwise touches a rupee figure — an LLM hallucinating a digit in an
amount field is the single most concrete way this project could cause
real financial harm, and the cheapest way to prevent it is to never let a
model near that field at all.

**Where enforced:** `src/execute/` builds the Payment Link request body
directly from the episode record's stored `amount_paise`; the LLM
interfaces in `src/diagnose/` and `src/choose/` never receive or return an
amount field, so there is nothing for a prompt-injected description to
overwrite even if it tried.

## Cap evaluation

**Decision refused:** whether an action would breach
`max_actions_per_payment`, `max_contacts_per_customer_7d`,
`batch_contact_ceiling`, or `per_run_exposure_ceiling_paise`
(`config/guardrails.yaml`).

**Why the model is kept out:** caps exist specifically to bound the blast
radius of a wrong decision elsewhere in the pipeline — including a wrong
LLM decision. A cap that the same model class could reason its way around
isn't a cap.

**Where enforced:** `src/gate/`, as deterministic counter comparisons
against SQLite-backed counts, run both before diagnosis (episode
eligibility) and again after action selection (post-selection re-check),
per the two-gate design in `CLAUDE.md`.

## Eligibility (the gate)

**Decision refused:** whether an episode is allowed into the pipeline at
all — the seven ordered checks in `src/gate/`.

**Why the model is kept out:** eligibility is where "no episode is ever
silently dropped" has to be provable. A rule a judge can read in
`config/guardrails.yaml` and `config/policy_table.yaml` is auditable in a
way a model's judgment call is not, and every refusal here gets written
to `gate_check` with a `reason` string, not a rationale a model composed
after the fact.

**Where enforced:** `src/gate/`, one function per check, each writing its
own `gate_check` row with `result` and `reason` before the next check
runs.

## Quiet hours

**Decision refused:** whether *now* is inside `quiet_hours` (21:00–09:00
IST) and therefore a hard block on contacting anyone, regardless of what
diagnosis or action was chosen.

**Why the model is kept out:** this is a wall-clock comparison against a
config value. There is no judgment call in it, and it is exactly the kind
of check that must never depend on a model being available, fast, or
having read the right timestamp correctly.

**Where enforced:** `src/gate/`, one of the seven ordered checks, using
`datetime.now(IST)` against `config/guardrails.yaml: quiet_hours`.

## Opt-out

**Decision refused:** whether a customer has opted out of recovery
contact — one of the four `hard_refuse_conditions` in
`config/policy_table.yaml`.

**Why the model is kept out:** an opt-out is a customer's explicit
instruction, recorded once. Re-litigating it via a model's read of the
episode context every time is both unnecessary and a place a wrong
inference could re-contact someone who explicitly said no.

**Where enforced:** `src/gate/`, checked against the `customer` table's
opt-out flag; `hard_refuse_conditions.customer_opted_out` in
`config/policy_table.yaml` is the config-level statement of the rule.

## Attribution

**Decision refused:** whether an outcome (a `payment_link.paid` event, or
its absence) counts as "recovered" for a given action, and within what
window.

**Why the model is kept out:** attribution is where the headline recovery
number comes from. `outcome_model.md` pre-registers the 48-hour window and
every response-probability assumption specifically so that number can't
quietly shift after the fact — a model re-deciding "was this really
caused by our action" episode-by-episode would reopen exactly the
question pre-registration exists to close.

**Where enforced:** `src/attribute/`, as a deterministic window
comparison (`config/guardrails.yaml: attribution_window_hours`, cross-
checked against `outcome_model.md` §3 by `make config-check`, check 8)
against `payment_link.paid` webhook timestamps.

## Any reported metric

**Decision refused:** the value of any number that appears in
`evidence/report.md` — guardrail correctness, admissibility rate,
throughput/cost, the recovery range.

**Why the model is kept out:** a report a model helped compute is a report
whose numbers a judge can't independently re-derive from the audit log.
Every reported metric here is a direct aggregation over `audit_record`,
`decision`, `execution`, and `ledger_entry` rows — arithmetic over
persisted facts, not a model's summary of them.

**Where enforced:** `scripts/eval.py` and `src/report/` read directly from
SQLite (`execution`, `exception_entry`, `decision`), the hash-chained audit
log, and the sealed split's pre-registered `response_probability` field —
no LLM call sits between a persisted row and a reported number.
`src/report/render.py` goes one step further than a convention: it refuses
to render a recovery-like figure that isn't an explicit (low, base, high)
range of integers (`BareRecoveryValueError`), so a bare point estimate
can't reach `evidence/report.md` even by accident.

## Action outside the admissible set

**Decision refused:** executing any action the policy engine didn't
explicitly admit for that `(cause_class, amount_band, segment,
instrument)` combination.

**Why the model is kept out:** this is the one place the LLM is *allowed*
to participate (`src/choose/` picks 1 of ≤3 actions the policy table
already admitted) but never to expand its own choices. If the model
returns something outside the admissible set, that is not a softer
failure to log and move past — `AdmissibilityError` halts the run.

**Where enforced:** `config/policy_table.yaml` defines the admissible set
per combination (27 explicit rules plus a conservative `default_rule`,
verified total by `make config-check`, check 4); `src/choose/` validates
the model's response against that set before it can reach `src/execute/`,
and `src/errors.py: AdmissibilityError` is the enforcement backstop if it
doesn't.

### The exact feature whitelist a selection prompt is allowed to see

`src/choose/selector.py: LLM_VISIBLE_FEATURES` is the complete, closed list
of episode-derived fields ever substituted into a selection prompt:

```
error_code, amount_band, segment, instrument, prior_contacts_7d, hours_since_failure
```

`amount_band` is the **band id** (`A1`/`A2`/`A3`), never the raw rupee or
paise value — the model never learns what `A2`'s upper edge actually is,
only which bucket this episode fell into. Nothing else reaches the prompt:
no cap value, no threshold, no ceiling, no guardrail name, no policy rule
text, and no amount in rupees or paise. The only other content in the
prompt is the admissible action ids themselves (plus a one-line, non-money
description of each) and the diagnosis's `class_id` and `confidence` —
neither of which is a guardrail or a threshold.

**Where enforced:** `tests/test_choose.py`'s
`test_rendered_prompt_contains_no_forbidden_tokens` renders a real
selection prompt against the real `config/` files and asserts it contains
none of `"5000"`, `"ceiling"`, `"cap"`, `"quiet"`, `"threshold"`, the raw
amount, or any `config/guardrails.yaml` key. If a model names a feature
outside this whitelist in its `features_used` response (which it cannot
be shown, so this would mean the model invented a field name), `select()`
logs it and records it on the `Selection` rather than crashing — an
interesting finding for the report, not a failure mode this project treats
as adversarial the way it treats an inadmissible `chosen_action` (see
"Action outside the admissible set" above).

## The idempotency key, and whether to retry

**Decision refused:** what the idempotency key for an execution is, and
whether a failed Payment Link call gets another attempt.

**Why the model is kept out:** the key is
`sha256(payment_id + ':' + policy_rule_id)[:32]` — a pure function of two
stable identifiers, deliberately *not* the webhook event id, because the
same payment generates multiple deliveries under different event ids and I
lost a session to that (see `BUILD_LOG.md`). A key a model derived would be
a key that could differ between two runs of the same episode, which is the
same thing as having no key: it is used as both the Payment Link's
`reference_id` and a SQLite `UNIQUE` constraint, and Razorpay itself
rejects a duplicate `reference_id` with a 400 — confirmed against the live
API on 2 Sep 2026, `evidence/razorpay_field_report.md` Step 5. Retry is the
same argument one level up: attempt count and backoff delay come from
`config/guardrails.yaml` and are written into the audit record
individually, so a judge can read what was attempted and when. A model
deciding "this one deserves one more go" would make that record fiction.

**Where enforced (tested):** `src/execute/idempotency.py` computes the key;
`tests/test_executor.py` asserts stability across runs
(`test_idempotency_key_matches_between_runs`) and that a second attempt
creates no link (`test_duplicate_suppression`). Backoff lives in
`src/execute/retry.py`, hand-rolled rather than `tenacity` precisely so each
attempt and delay is an explicit line in the audit record.

## Whether to stop the run

**Decision refused:** whether a batch halts — on consecutive executor
errors, on a cap breach, on a shared-cause cluster above threshold, or on
the kill-switch file being present.

**Why the model is kept out:** a stopping rule exists to bound damage when
something upstream is already wrong, and the model is one of the things
that could be wrong. A halt condition that consults the same class of
component it is supposed to protect against is decorative. These are also
the rules a panel is most likely to ask me to demonstrate firing, and "it
fired because the model judged the situation had deteriorated" is not a
demonstration.

**Where enforced (tested):** `src/gate/stopping.py`, as integer comparisons
against `config/guardrails.yaml`; every stop writes a `stage="stop"` audit
record naming which rule fired, and `tests/test_failure_paths.py` exercises
them. The cluster threshold is the one number here with an experiment
behind it — `experiments/thresholds/outage_cluster.md`.

## Which escalation tier an episode lands in

**Decision refused:** whether an episode is `auto`, `human_keystroke`, or
`hard_refuse`, and the named reason recorded for that assignment.

**Why the model is kept out:** this is the decision that determines whether
a human being is asked to press a key before a money-adjacent action is
taken. It is a comparison of `amount_paise` against
`auto_approve_ceiling_paise`, and of the running contact count against
`batch_contact_ceiling` — both config values the selection prompt is
specifically never shown. If the model could influence its own tier, it
could route its own choice around the human gate, which inverts the entire
control.

**Where enforced (tested):** `src/gate/engine.py`'s `_compute_tier()` and
the `TierReason` constants; the tier and its named reason are written to
every gate audit record. `tests/test_choose.py` renders a real selection
prompt against the real `config/` files and asserts it contains none of
`"5000"`, `"ceiling"`, `"cap"`, `"quiet"`, `"threshold"`, the raw amount, or
any `config/guardrails.yaml` key — the model cannot reason about a
threshold it is never told.

## Webhook authenticity and deduplication

**Decision refused:** whether an inbound webhook is genuine, whether it has
been seen before, and whether it arrived out of order.

**Why the model is kept out:** signature verification is an HMAC
comparison. There is no interpretation in it, and a model in that path
would add latency to the one endpoint with a hard sub-50ms budget while
making a security boundary probabilistic. Dedup is a `UNIQUE` constraint on
`payment_id`; ordering is a lookup for a prior `payment.failed`. All three
have exactly one right answer.

**Where enforced (tested):** `src/ingest/signature.py` and
`src/ingest/service.py`; `tests/test_ingest.py` covers a valid signature, a
replayed event id, the same payment arriving under a different event id,
and a `payment.captured` with no prior failure — recorded `out_of_order`
with no recovery episode created.

## The sealed split

**Decision refused:** which episodes are sealed, and whether the seal is
intact.

**Why the model is kept out:** the seal is a sha256 over
`holdout/sealed.jsonl`, committed before the eval harness existed. Its
entire value is that it is mechanical and checkable by someone who has no
reason to trust me. `scripts/holdout_guard.py` goes further and refuses to
let `holdout/labels.jsonl` be opened before the pipeline has finished — the
agent never reads outcome labels, and that is a code-level guard rather
than a promise in a README.

**Where enforced (tested):** `make verify-seal`;
`tests/test_holdout_guard.py`.

## What the model *is* allowed to do, stated plainly

Two things, and I want them on the same page as the refusals so the balance
is visible:

1. **Classify.** Map a heterogeneous issuer error string onto one of nine
   canonical causes, with a confidence and a one-line rationale. The regex
   baseline runs first and resolves most of them for free; only the
   unmatched tail reaches the model. `evidence/report.md` section 3 reports
   where this loses, including the top five error families by volume where
   free regex ties it at 100%.
2. **Select.** Pick one action from an admissible set of at most three that
   the policy table has already fixed, naming the features it used.

Both are language and pattern work over an input that has already been
validated, and neither can widen its own options. Everything downstream of
the selection — whether it is permitted, what it costs, whether it
executes, whether it worked — is deterministic.

## The closing argument

Every line in this document is a place I could have reached for the model
and chose not to, and each refusal cost me something concrete: the policy
table is 27 hand-written rules instead of a prompt, the taxonomy is
anchored to 20 harvested strings instead of nine I invented, and the tier
logic is two integer comparisons where a model could have been more
nuanced.

I think that trade is right, and my reason is narrower than "LLMs are
unreliable." It is that this system's only defensible claim is its audit
trail. Every reported number has to be re-derivable from persisted rows by
someone who does not trust me — that is what the hash chain, the
pre-registered outcome model and the sealed split are all for. A decision a
model made is not re-derivable. It is reproducible only in the weak sense
that the same prompt tends to give the same answer, and I cache the
responses precisely because "tends to" is not good enough. The moment a
model touches a cap, an amount, or an attribution, the audit log stops
being a record of what the system did and becomes a transcript of what it
said it did.

So the boundary is not drawn around what the model does badly. It is drawn
around what has to stay checkable. That is why `src/diagnose/` and
`src/choose/` are the only two packages on the model's side of it, and why
`tests/test_llm_boundary.py` fails the build rather than logging a warning
if that ever stops being true.

## Anything that moves money

**Decision refused:** everything, categorically. No code path in this
project moves money. The only external effect the system produces is a
*cancellable* Razorpay Payment Link with `expire_by` set, which the
customer authenticates themselves.

**Why the model is kept out:** this isn't really a "keep the LLM out of
one decision" case — it's the constraint every other section in this
document is downstream of. There is no decision in this system, model-made
or otherwise, whose exercise directly debits or credits anyone.

**Where enforced:** `src/execute/` only ever calls Razorpay's Payment
Links API, never a transfer, payout, or capture endpoint; this is a
structural property of which SDK/API calls exist in the codebase, not a
runtime check — `grep -rn 'payouts\|transfers\|\.capture(' src/` is
expected to return nothing beyond the (Razorpay Payments API's own)
`payment_id` capture-status field.
