# Limitations

What is simulated, what is assumed, and what breaks at scale. Kept honest and
updated as each phase surfaces a new one — see `BUILD_LOG.md` for the session
each entry came from.

## Razorpay test-mode account: 30 Payment Link cap (resolved 30 Aug 2026)

**Current state, as of 30 Aug 2026: resolved.** The cap reset on its own —
no documented reset window was ever found, so this wasn't fixed, it just
stopped being true. Phase 12's attribution work confirmed it live (a real
create + cancel succeeded on a throwaway link) and then created 9 more real
Payment Links via `make demo --execute`, watched them, and rolled all 9 back
cleanly. The "three real `plink_` IDs created live" acceptance step is
demonstrable again. See `BUILD_LOG.md`'s "D6 (attribution)" entry for the
full story, including how this was caught. The account being test-mode
means this state itself is not guaranteed to hold — if a submission run
hits `RATE_LIMIT_EXCEEDED` again, the section below is what that means and
what still works regardless.

Phase 1's harvest (forcing real checkout failures via Razorpay's mock bank to
capture genuine `error_code`/`error_reason` strings) and later live-execution
testing (Phase 8) both create real Payment Links / orders against this test
account. Razorpay enforces a documented cap of 30 Payment Links per test-mode
account; confirmed empirically in Phase 8 via the API's own error body —
`{"code": "RATE_LIMIT_EXCEEDED", "description": "test mode limit of 30
reached for payment_link"}` — not inferred. See
`evidence/razorpay_field_report.md` Step 5 for the full account, including an
earlier, wrong guess (a time-windowed limit rather than a hard cap) that this
finding corrected.

**Consequence:** this account cannot create a new real Payment Link until
Razorpay resets the cap. No documented reset window was found in Razorpay's
docs as of 27 Aug 2026. `src/execute/executor.py`'s `RazorpayExecutor` is
fully unit-tested against a mocked client (`tests/test_executor.py`, 14
tests) and its dry-run path is verified live to make zero HTTP calls; the one
`@pytest.mark.live` test that creates a real link will legitimately skip or
fail while the cap is exhausted, which is expected, not a bug — it is
excluded from `pytest -m "not live"`, the run `make eval` and this project's
default test invocation both use.

**What this means for the submission:** the "three real `plink_` IDs created
live" acceptance step cannot be re-demonstrated on this account right now.
What *is* demonstrated against the real API, real 429 included: the
hand-rolled backoff (1s → 2s → 4s, config-driven from
`config/guardrails.yaml`, not hardcoded), the retry cap stopping at 3
attempts, and the episode landing in the exception list rather than crashing
the batch — the same behavior the phase's fault-injection acceptance test
independently asks for, triggered here by a real account limit instead of a
synthetic one.

Razorpay's documented server-side rejection of a duplicate `reference_id`
(a 400 response, per its Payment Links "Create" error-list docs) is
implemented defensively in `RazorpayExecutor.create_recovery_link` — treated
identically to local idempotency dedup — but is untested against the live
API for the same reason: it requires a successful creation to duplicate
against.

## What breaks at 10k episodes/day (design-level, not yet load-tested)

Not yet validated under load; named here as known architectural limits, to
be expanded as later phases touch throughput:

- The in-process dedup/idempotency store is a single SQLite file in WAL mode
  — fine for a batch replay, not for concurrent high-throughput webhook
  ingestion.
- The executor has no per-issuer circuit breaker — a single issuer's outage
  currently only trips the cluster-escalation stopping rule at the batch
  level, not a targeted per-issuer backoff.
- The audit chain is a single append-only file per run; verification walks
  it linearly, which does not partition well past a few hundred thousand
  records.

## Attribution (src/attribute/): what AR-01 does not count as recovered

AR-01 (`src/attribute/rules.py`) is deliberately conservative about what it
will claim credit for. Three cases are always reported as `not_recovered`,
each with its own reason code, rather than being counted toward the
recovery figure:

- **Partial payments (`partial_payment_not_attributed`).** If the amount
  actually paid against a recovery link is less than the amount the link
  was created for, the episode is not attributed as recovered — Second
  Rail does not currently support partial-recovery accounting (crediting a
  fraction of the recovered amount), so a partial payment is treated the
  same as no payment for the recovery figure, even though the merchant did
  receive some money. This undercounts gross recovery in the (rare, for a
  single fixed-amount Payment Link) case a customer pays less than asked.
- **Recovery via a channel this run did not create
  (`unattributable_recovery`).** If a payment or order later shows as paid
  but the event does not reference the Payment Link this run's executor
  created (different `plink_id`, no `order_id` match), Second Rail does
  not claim it — the customer may have paid through some other channel
  entirely, and claiming credit for that would overstate the system's
  effect.
- **A terminal event arriving after the attribution window
  (`outside_attribution_window`).** See outcome_model.md §3 — a payment
  completed on hour 49 of a 48-hour window is recorded as not-recovered by
  this accounting, even though the action may in fact have caused it. The
  window is a measurement boundary, not a claim about when customers stop
  responding.

## Eval harness (`scripts/eval.py`, `src/report/`): how the sealed batch is scored

The sealed split has no real customer to actually pay a link — there is no
`payment_link.paid` webhook to listen for, because nothing generated one.
`scripts/eval.py` therefore does not use `src/attribute/`'s real webhook-
driven `OutcomeWatcher` for its recovery figure at all; it substitutes a
simulation, and every simplification that substitution makes is listed
here rather than left implicit:

- **Recovery is an expected value, not a resampled outcome.** Sigma
  (response_probability x amount_paise) over contacted episodes, using
  each sealed episode's own pre-drawn `response_probability` — not
  `holdout/labels.jsonl`'s `responded` boolean. A single boolean draw per
  episode on a 200-episode batch would make the +/-30% sensitivity sweep
  measure sampling noise more than the swept parameter; see
  `src/report/sensitivity.py`'s module docstring for the full reasoning.
  This means the reported recovery figure is *never* going to exactly
  equal what `holdout/labels.jsonl`'s booleans alone would sum to.
- **The attribution-window sweep is a structural no-op.** The
  expected-value method above has no elapsed-time signal — no real
  `payment_link.paid` timestamp exists to compare against a window — so
  widening or narrowing the 48h window changes nothing in this specific
  computation. Swept anyway, for disclosure completeness (it is one of
  the three pre-registered parameters), with that no-op stated plainly in
  `evidence/report.md` itself, not hidden.
- **The false-positive count is reported as exactly what the gate
  already guarantees, and nothing more exotic.** `compute_fp_cost()`
  (`src/attribute/ledger.py`) counts contacts made to a customer who was
  already paid, opted out, or over the frequency cap *as of contact
  time* — and since the sealed run's own gate already refuses exactly
  those three conditions before an execution row can exist, this is
  reliably zero for a single deterministic batch replay, the same
  "zero by construction" property `evidence/guardrail_proof.json`
  documents for cap breaches and quiet-hour contacts.
- **The FIXED_RETRY_AT_T30 baseline reuses the same simulated response
  draw as Second Rail**, on the theory that `outcome_model.md`'s
  response-probability formula depends on `(cause_class, segment,
  amount_band)`, not on which specific plausible retry action a customer
  was offered — so the same underlying customer, contacted by either
  policy, is modelled as equally likely to respond. The baseline's own
  "no diagnosis, no policy table" nature is expressed entirely through
  *which* episodes get contacted (every gate-eligible one, with no
  `no_action` suppression), not through a different response model.
- **Classifier confusion cost (`evidence/report.md` §6) is not a live
  LLM re-selection.** For each observed (true, predicted) confusion, the
  reported rupee delta uses the deterministic
  `fallback_priority`-first admissible action under each class — the
  same fallback `src/choose/selector.py` uses when the LLM is
  unreachable — not a real selection prompt re-run under the wrong
  class. Cheaper and reproducible; disclosed as a simplification in the
  report itself, not fabricated as a live measurement.
- **A real sealed-batch run of `make eval` found the false-positive count
  is not always exactly zero, and the reason is a genuine artifact of
  batch replay, not a bug in the gate.** `src/gate/checks.py`'s
  frequency-cap check reasons about each episode's own `failed_at`
  timestamp (spread across the generator's ~30-day window), while
  `compute_fp_cost()` (`src/attribute/ledger.py`) independently recounts
  prior contacts using each `execution.created_at` timestamp — the
  moment the batch actually ran, which is nearly identical wall-clock
  time for every episode in a fast batch replay. Two episodes for the
  same customer with `failed_at` values ten days apart are correctly
  judged outside each other's 7-day frequency window *at gate time*, but
  both get executed within the same few seconds of real batch-processing
  time — so the post-hoc, execution-time-based false-positive check can
  (rarely) flag a contact the gate's own historical-time reasoning
  already allowed. A live system, where gate time and execution time are
  the same clock, would not exhibit this. Not fixed, because the two
  checks are reasoning about two different, both-legitimate notions of
  "now" — gate-time eligibility (was this OK to contact given what was
  known then) versus audit-time correctness (does the persisted contact
  history actually violate the cap) — and collapsing them would require
  either gate checks to know the batch's real wall-clock execution time
  in advance, or the audit to reason about simulated episode-time instead
  of real timestamps. Left as an honest, understood limitation of
  evaluating a historical batch in a few seconds rather than in real
  time.

  **Checked, not assumed: this is not the same bug as the
  exposure/contact-counter accounting fix documented in BUILD_LOG.md.**
  When that fix landed,
  the natural question was whether it also explained this fp_count=1 —
  a `no_action` episode wrongly inflating `contacts_by_customer` would
  make the *gate* over-suppress, not make the post-hoc audit flag a real
  contact as a false positive, but the two are different-enough
  mechanisms that this was verified empirically rather than reasoned
  through on paper alone. After the fix, `fp_count=1` with
  `breakdown={'frequency_cap_exceeded': 1}` persists **identically** in
  both the Second Rail run and the FIXED_RETRY_AT_T30 baseline — and the
  baseline never had a `no_action` path to begin with (`choose_enabled`
  is always `False` there, so its `action` is always
  `"placeholder_action"`, never `"no_action"`; the counter fix is a
  structural no-op for it). Identical behaviour in a run the fix cannot
  possibly touch confirms the clock-mismatch explanation above is the
  actual, sufficient cause.

## `make guardrail-proof` cannot reach N=200 on this account, right now

Two separate, independent constraints, neither a code bug:

- **`data/train.jsonl` only has 108 gate-eligible episodes out of 400.**
  `scripts/guardrail_proof.py`'s `select_gate_eligible_slice()` needs N
  gate-eligible episodes to build a fixed, deterministic slice, and
  `--n 200` fails immediately (before any real API call) with
  `RuntimeError: only found 108 gate-eligible episode(s)`. 108 is the
  practical ceiling this dataset supports for this specific tool.
- **This account is under real, sustained Razorpay-side rate-limiting**
  from this project's own cumulative test-mode volume across build
  sessions — confirmed by the actual response bodies real 429s carry
  (`{"error": {"description": "Too many requests", "code":
  "BAD_REQUEST_ERROR"}}`, distinct from this tool's own synthetic
  fault-injection bodies), not inferred. `RazorpayClient`'s self-imposed
  0.5 req/s token bucket is confirmed active on every real call in this
  path (checked directly, not assumed) and is not being bypassed
  anywhere; the account's real limit is simply tighter than that right
  now. Raised `guardrail-proof`'s own `consecutive_executor_errors_stop`
  tolerance to 5 (from the shared production default of 3, which is
  unchanged in `config/guardrails.yaml`) to absorb some of this, but a
  real run on 2026-09-01 still only reached 10/108 before the raised
  tolerance was hit.

**What this means for the reported guardrail-correctness numbers:**
they are real and genuinely verified against the live Razorpay API (0
duplicate links, 0 cap breaches, 0 quiet-hour contacts, idempotency
collisions correctly detected — most recently 10/10) — just over a
smaller N than the 200 originally targeted. `evidence/report.md` §1
states the actual N and the stopping reason on the page rather than
implying fuller coverage than what happened. If the account's real rate
limit eases (it may be tied to a rolling window, not a hard cap — not
confirmed either way), a re-run of `make guardrail-proof N=108` could
reach further; this has not been retried repeatedly to avoid adding to
the same cumulative volume causing the throttling in the first place.
