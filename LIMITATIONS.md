# Limitations

What is simulated, what is assumed, and what breaks at scale. Kept honest and
updated as each phase surfaces a new one — see `BUILD_LOG.md` for the session
each entry came from.

## Razorpay test-mode account: 30 Payment Link cap, exhausted

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
