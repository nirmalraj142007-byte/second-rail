# Outcome Model — Pre-Registration

This file states every assumption feeding the recovery figure, committed
before any eval code exists and before the first eval run. Its git timestamp
is the pre-registration proof: `git log --format='%ad %s' --date=short` must
show this file landing in commit 1, ahead of anything under `src/attribute/`
or `src/eval*`.

The recovery figure Second Rail reports is not a measurement of the real
world — it is the output of a simulator whose assumptions are the numbers
below. Nobody should trust it more than they trust this table. That is by
design: §2 and §3 of this document are more load-bearing than the number
itself, and the report leads with the metrics that don't depend on either.

## 1. Purpose

Every response probability, every cost figure, and every window used to
compute "rupees recovered" is declared here, with its reasoning, before a
single eval episode runs. Nothing in `src/attribute/` may introduce a
parameter that isn't listed here. If a number in the report can't be traced
to a row in this file, that's a bug in the report, not a missing row here.

## 2. Customer-response probability model

Structure: for each `(cause_class, segment, amount_band)` triple, a
probability that the customer completes the offered recovery action
(re-attempts payment via the link) within the attribution window, and the
reasoning basis for that probability.

Cause classes are `TBD` pending Phase 4 (`config/taxonomy.yaml`), where the
nine-class taxonomy gets frozen against harvested Razorpay error strings —
see judge expectations §3 [W][HARD] and blueprint U-14 area on taxonomy
provenance. The table structure and reasoning method are fixed now so that
Phase 4 only fills in class names and re-derives probabilities per class,
never invents new columns.

| cause_class | segment | amount_band | response_probability | reasoning |
|---|---|---|---|---|
| TBD (Phase 4) | first_time | low (< ₹1,000) | TBD | Transient/technical causes (timeout, issuer decline) get a higher assumed response than customer-abandonment causes (drop-off at auth), because the failure wasn't a change-of-mind. First-time customers get a lower base rate than repeat, on the assumption that trust in the merchant is a factor in returning to finish a payment. |
| TBD (Phase 4) | repeat | low (< ₹1,000) | TBD | Repeat customers get a higher base rate than first-time at the same amount band and cause — same reasoning, applied as a segment multiplier on the cause's base rate. |
| TBD (Phase 4) | high_value | low (< ₹1,000) | TBD | High-value segment (top 15% by historical spend) assumed to respond faster and more reliably to a fresh link, regardless of amount band, on the assumption that they have stronger purchase intent. |
| TBD (Phase 4) | first_time | mid (₹1,000–₹10,000) | TBD | Response probability is assumed to fall as amount rises within every segment — larger amounts carry more reconsideration risk. |
| TBD (Phase 4) | repeat | mid (₹1,000–₹10,000) | TBD | Same amount-decay assumption, applied on top of the repeat-segment multiplier. |
| TBD (Phase 4) | high_value | mid (₹1,000–₹10,000) | TBD | Same amount-decay assumption, applied on top of the high-value multiplier. |
| TBD (Phase 4) | first_time | high (> ₹10,000) | TBD | Steepest amount-decay penalty; large-amount recoveries are the least-trusted number in this table and should carry the widest sensitivity band. |
| TBD (Phase 4) | repeat | high (> ₹10,000) | TBD | Same, with repeat-segment multiplier. |
| TBD (Phase 4) | high_value | high (> ₹10,000) | TBD | Same, with high-value multiplier. |

**Reasoning method (fixed now, applied in Phase 4):** each cause class gets
one base response rate, chosen by whether the underlying cause is
transient/technical (higher assumed rate — the customer didn't choose to
abandon) or intent-related (lower assumed rate — the customer changed their
mind or was blocked for a reason likely to recur). That base rate is then
adjusted by two multipliers, applied independently: a segment multiplier
(high_value > repeat > first_time) and an amount-band decay (probability
falls as amount rises). No cause/segment/amount cell is measured — every
cell is this base rate times these two multipliers, and that derivation, not
the specific numbers, is what the ±30% sensitivity sweep in Phase 7
perturbs.

## 3. Attribution window

**48 hours from action execution.**

Neither source document states a number — proposal §3 says "within the
attribution window" without one, and the judge file marks an undefined
window as an outright fail (§1, "Measured money recovered across a batch" →
Fails clause). 48 hours is the default this document commits to absent
other instruction, justified as follows:

- The guardrail cap on episode age for action is 72 hours
  (`config/guardrails.yaml`, Phase 3). An attribution window equal to or
  longer than that cap would let an episode's outcome get attributed after
  it would already be ineligible to action again — a window shorter than the
  action-age cap avoids that inconsistency.
- 48 hours covers a full weekend gap (Friday failure, Sunday resolution)
  without covering so much time that a coincidental unrelated payment from
  the same customer gets misattributed as the action's outcome.
- It is short enough that a demo run (batch replay, not real time) can
  compress the window to a config-overridable value for testing without the
  production default ever being ambiguous.

If a real distribution of `payment_link.paid` latencies is harvested in
Phase 5 alongside the error strings, this section gets a dated appendix
entry, not a silent edit — see §6.

## 4. False-positive cost model

Every contact sent to a customer who had already paid, opted out, or was
contacted within the frequency cap window in the last 7 days counts as a
false positive and is priced against gross recovery to produce a net figure.

- **SMS cost: ₹0.20 per notification.** Stated in the proposal (§6) as the
  cost of a Payment Link's `notify.sms` flag firing in test mode. Treated as
  a fixed, non-simulated unit cost — this is the one row in this section
  that is not an assumption, it is a quoted per-message price.
- **Goodwill cost: ₹15 per false-positive contact. ASSUMPTION.** Basis: an
  unwanted "you have a pending payment" nudge to someone who already paid or
  opted out is a minor trust cost, not a support-ticket-generating one — it
  does not carry the cost of an incorrect debit or a wrong charge. ₹15 is
  set at roughly 75x the SMS cost itself, reflecting that the reputational
  cost of an unwanted nudge is assumed to dominate the delivery cost by two
  orders of magnitude, but is still a small fraction of the median
  transaction amount (₹850, per the proposal's illustrative distribution) —
  intentionally small enough that it cannot on its own flip a net-positive
  batch to net-negative, and large enough that a policy with a high
  false-positive rate visibly loses money in the report rather than being
  free to over-contact. No survey, support-ticket log, or churn data backs
  this number; it is a named guess, and the report labels it as such
  wherever the net figure appears.
- **Net recovery = gross recovered − (false positives × (₹0.20 + ₹15)).**
  Reported as the headline recovery number; gross is never shown without
  the net figure beside it.

## 5. What this model cannot tell you

- **Whether a real customer would actually re-attempt payment.** Every
  response probability in §2 is an assumption, not a measurement of human
  behavior — see judge expectations §3 on why a ±30% sweep of self-authored
  parameters does not convert this into evidence, only discloses it
  honestly.
- **Whether the goodwill cost of a false positive is ₹15, ₹1.50, or ₹150.**
  There is no support-ticket or churn data behind that figure in a 33-hour
  synthetic-data build; it is a named guess, not a measurement.
- **Whether the response-probability differences between segments
  (first-time / repeat / high-value) reflect anything about real customer
  behavior**, as opposed to a plausible-sounding ordering chosen by the
  builder. The ordering is a design assumption, stated as one, not derived
  from any dataset.
- **What happens outside the 48-hour attribution window.** A customer who
  pays on hour 49 is recorded as not-recovered by this model's accounting,
  even though the action may have caused the payment. The window is a
  measurement boundary, not a claim about when customers stop responding.
- **Anything about instruments, causes, or amount distributions not present
  in the seeded generator.** The model has no signal about real Razorpay
  merchant traffic; it only knows what `data/generator.py` was told to
  produce.

## 6. Amendment policy

This file is never edited in place once committed. Any correction —
including resolving the `TBD` cells in §2 during Phase 4 — is written as a
new, dated entry in an **Appendix** section appended to the bottom of this
file, in its own commit, referencing what changed and why. The original
text above stays untouched so git history shows the actual sequence of
assumptions, not a cleaned-up final version presented as if it were always
correct.

## Appendix — 2026-08-26: §2 response-probability parameters filled in (Phase 5)

Phase 4 (`config/taxonomy.yaml` ratification, commits `55d39c5` /
`f67d9ad`) fixed the nine cause-class names but did not fill this
section's `TBD` cells — that commit closed the class-*identity* question
only. Phase 5 (`data/generator.py`) is the first place a concrete number
is needed, because `holdout/labels.jsonl` has to record a simulated
customer-response draw per sealed episode. This entry fills the `TBD`
cells using exactly the reasoning method §2 already committed to, and
nothing else — no new column, no new logic.

**Base response rate per cause class** (transient/technical causes get a
higher rate; intent- or limit-related causes get a lower one, per the
reasoning method above):

| cause_class | label | base_rate | reasoning |
|---|---|---|---|
| C7 | payment_timed_out | 0.70 | Purest transient cause — nothing about the instrument or customer is implicated. |
| C8 | issuer_bank_technical_error | 0.65 | Upstream fault, same reasoning as C7, one notch down because the fault sits at the bank rather than the immediate connection. |
| C4 | authentication_failed | 0.60 | A single OTP/3DS miss is usually a one-off, not a standing problem. |
| C9 | device_or_app_unreachable | 0.55 | Device/app reachability is usually transient but ties recovery to the customer fixing something on their end first. |
| C3 | invalid_entered_details | 0.50 | Fixable data-entry mistake, but requires the customer to notice and correct it. |
| C1 | insufficient_funds | 0.45 | Recoverable in principle, but requires funds to actually become available. |
| C5 | limit_or_attempts_exceeded | 0.35 | Recoverable only after a limit window resets — the customer can't act immediately even if willing. |
| C2 | issuer_declined | 0.30 | The instrument itself is impaired; retrying anything is less likely to help without switching instruments. |
| C6 | customer_abandoned | 0.20 | Intent-related — the customer chose to stop. Lowest assumed rate in the table. |

These are the same `response_base_rate` values now committed per class in
`config/taxonomy.yaml`, alongside the pre-existing `generation_weight`
field `data/generator.py` uses for the cause mix. Both are declared
illustrative in that file's own header, same as this table.

**Segment multiplier** (high_value > repeat > first_time, per §2's
reasoning): first_time ×0.85, repeat ×1.00, high_value ×1.15.

**Amount-band decay** (probability falls as amount rises, per §2's
reasoning): rather than introduce a fourth banding scheme alongside the
low/mid/high sketch in §2's original table, this reuses
`config/policy_table.yaml`'s three amount bands (A1/A2/A3) — the same
bands the policy engine already resolves every episode into, so
`data/generator.py` doesn't maintain a second boundary set. A1 ×1.00, A2
×0.85, A3 ×0.65.

**Formula:**

```
response_probability = clip(
    base_rate[cause_class] * segment_multiplier[segment] * amount_decay[amount_band],
    0.02, 0.95,
)
```

The 0.02/0.95 floor and ceiling exist so no cell is ever a hard 0% or
100% — consistent with §5's disclosure that no cell is measured, every
cell is a formula output. The full 9 × 3 × 3 = 81-cell table §2 sketched
is derivable from this formula on demand; it is not transcribed here.

**What changed and why:** only the `TBD` cells in §2's table are
resolved, via the parameters above — the original table's illustrative 9
rows are superseded by this formula, which covers the same 9
(segment, amount_band) shape once a cause class's base rate is
substituted in. §2's text above this appendix is left untouched, per the
amendment policy in §6. This does not change the attribution window (§3)
or the false-positive cost model (§4).
