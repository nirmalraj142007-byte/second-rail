# Where the LLM is not

*First draft — Phase 4. Updated Phase 7: `src/gate/` is now real, and
`tests/test_llm_boundary.py` is the enforcement mechanism referenced
throughout this document — it walks `src/gate/`, `src/execute/`,
`src/attribute/`, `src/audit/`, `src/ingest/`, `src/db/` and fails the
build if any file in them imports the LLM client module or contains the
strings `"openai"`, `"genai"`, or `"anthropic"`. `src/execute/` and
`src/attribute/` don't exist yet — everywhere else I name a path that
isn't built, I'm naming where the boundary will be enforced once that
phase lands, per the module map in `CLAUDE.md`.*

Second Rail calls an LLM twice per episode, at most: once to classify a
cause when the regex baseline doesn't match, once to pick one action from
an already-narrowed set of at most three. Everywhere else in this system,
the LLM is refused outright — not "discouraged," refused, with a test that
greps the package for the client symbol and fails the build if it's there
(`test_llm_boundary.py`). This document is the list of exactly which
decisions that refusal covers, and why each one is a decision I don't
trust a model to make.

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

**Where enforced:** the (not-yet-built) eval/report pipeline reads
directly from SQLite and the hash-chained audit log; no LLM call sits
between a persisted row and a reported number.

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
