# Where this breaks at 10k episodes/day

Three places. In the order I would fix them, which is not the order of
severity — it is the order of *how much is unrecoverable if it goes wrong*.
A dropped webhook is a payment nobody ever looks at again. A slow audit
verification is an inconvenience. So the queue goes first.

10k episodes/day is roughly 7 per minute averaged, but Indian payment
traffic is bimodal (lunch and evening), so the peak is what matters — call
it 30-40/minute sustained for a couple of hours. None of this has been load
tested. These are design-level claims about code I wrote, and I have tried
to state them narrowly enough that I can defend each one rather than
gesture at it.

---

## 1. The in-process webhook queue loses events on restart

**Where it is:** `src/ingest/app.py`. `POST /webhooks/razorpay` verifies the
HMAC signature synchronously, parses just enough of the envelope to find
`event` and `X-Razorpay-Event-Id`, pushes a dict onto a `queue.Queue`, and
returns 200. A single worker thread drains that queue and does the real work
— dedup, normalization, the SQLite writes — in `src/ingest/service.py`. That
split is what keeps the endpoint under the 50ms target.

**The symptom, specifically.** The queue is in process memory and unbounded.
Two distinct failures come out of that:

- **Loss on restart.** Every item still in the queue when the process exits
  is gone, and Razorpay has already been told 200, so it will never redeliver.
  At 40 events/minute with a worker doing SQLite writes in WAL mode, the
  steady-state depth is small — but a deploy, an OOM kill, or an unhandled
  exception in the worker loop drops whatever is in flight. The events that
  vanish are exactly `payment.failed` events, which is to say the input to
  the entire product.
- **Unbounded growth under back-pressure.** The queue has no `maxsize`. If
  the worker slows down — SQLite writer lock contention, a slow disk — the
  producer never blocks and never sheds. Memory grows until the process dies,
  which then triggers the first failure at its worst possible moment: maximum
  queue depth.

There is also a hard structural ceiling behind both: one worker thread owning
one SQLite connection, because `sqlite3` connections are single-threaded by
default and this codebase does not turn that off. Adding threads does not
help while the writer is one file.

**The fix.** Replace the in-process queue with a durable one and make the
endpoint's 200 mean "persisted", not "accepted". Concretely, in the order I
would do it:

1. Write the raw envelope plus the event id to a `webhook_inbox` table
   *inside the request*, in one INSERT, before returning 200. This is cheap
   (one write, no parsing) and it makes redelivery unnecessary because
   nothing is ever only in memory. The existing `webhook_event` UNIQUE
   constraint on the event id already gives idempotent re-insert.
2. Move the worker to a separate process polling that table, so a deploy of
   the API does not interrupt draining.
3. Only then reach for Redis or SQS, and only for fan-out across multiple
   workers — at which point SQLite has to go too, which is why this is step
   three and not step one.

**What I would measure to know it worked:** kill -9 the process mid-burst and
assert `count(webhook_inbox) == count(events sent)`. Today that assertion
fails; that is the whole point of the fix.

---

## 2. The executor has no per-issuer circuit breaker

**Where it is:** `src/execute/executor.py` and `src/gate/stopping.py`. Retry
and backoff are per-call (cap 3, delays 1s/2s/4s, from
`config/guardrails.yaml`). The only issuer-aware logic anywhere is the
outage-cluster check in `src/gate/engine.py`, which suppresses and escalates
when more than `outage_cluster_threshold` (15) episodes share an
`error_reason` inside a 30-minute window.

**The symptom, specifically.** Those two mechanisms both have the wrong
granularity for a real single-issuer outage at volume.

- The cluster check is a *batch-level* refusal computed once, before any
  episode is gated, over the episodes in that batch. It answers "is this
  batch dominated by one cause?" It does not answer "is BANK_C failing right
  now?" A stream of 10k/day arriving continuously never forms the batch the
  check is written against.
- The consecutive-executor-errors stopping rule (3) is *global*. If BANK_C is
  down and the other six issuers are fine, the interleaving means you rarely
  get three consecutive failures — so the rule never fires — while every
  BANK_C episode burns its full retry budget: 3 attempts and 7 seconds of
  backoff each, forever, against an endpoint that is going to keep failing.
  At 40/min with 20% BANK_C share, that is ~24 wasted calls per minute
  indefinitely.
- Worse, it is not merely wasteful. Razorpay rate-limits, and I have the
  429s to prove it — the guardrail-proof run in `evidence/report.md` reached
  10 of 108 requested episodes before real Razorpay-side rate limiting
  tripped its stopping rule. Hammering a dead issuer spends the rate-limit
  budget that the six healthy issuers need.

**The fix.** A per-issuer circuit breaker in front of `create_recovery_link`,
keyed on the issuer family already present on the episode:

- Closed -> open after N failures for that key inside a rolling window
  (start at the same 3 the global rule uses, then run the same style of
  sweep `experiments/thresholds/retry_cap.md` uses, because I do not want a
  second arbitrary threshold in this system).
- While open, skip the call entirely and record the episode as suppressed
  with a distinct `reason_code` — `issuer_circuit_open` — so it lands in the
  exception list rather than disappearing, which is this project's standing
  rule.
- Half-open after a cooldown: let exactly one call through and close on
  success.

The state belongs next to the frequency-cap counters, so it should be a
table, not a dict — the executor already has the connection.

**The honest caveat:** I have not built this, so I am claiming a design, not
a result. What I *can* show today is the failure it addresses, in a real run
against the real API, in the report.

---

## 3. The audit chain is one linear file per run

**Where it is:** `src/audit/writer.py` writes append-only JSONL, one record
per decision, each hash-chained to the previous. `src/audit/verify.py --all`
walks every file and prints `chain intact - N records`.

**The symptom, specifically.** Verification is O(total records) and it is a
single sequential pass, because that is what a hash chain is. Today that is
15,605 records in about 0.4 seconds. At 10k episodes/day, one episode
produces roughly 12 records (7 gate checks plus diagnose, choose, the
post-selection re-check, execute, attribute) — call it 120k records/day, so
about 44 million/year. At the current rate that is a verification pass
measured in tens of minutes, and it is unavoidable: you cannot verify half a
hash chain, and `make verify-audit` is a demo beat that has to finish in
under two seconds.

The second-order problem is retention. The chain is one continuous structure,
so you cannot delete or archive 2026's records without breaking the link into
2027's. A system that can never drop a record is a system that eventually
cannot afford to keep them.

**The fix.** Partition the chain by run, which the file layout already half
does — records are written per run to `evidence/audit/<run_id>.jsonl` — and
make the partitioning *semantic* rather than incidental:

- Each run's chain starts at its own genesis hash and ends with a sealed
  record carrying the run's final hash and record count.
- A small `chain_head` table holds one row per run: run id, first hash, last
  hash, count. That table is itself chained.
- `make verify-audit` then verifies the head table (cheap, one row per run)
  plus whichever runs you name. Verifying everything stays possible and
  becomes embarrassingly parallel — each run's file is independent.
- Archiving becomes possible too: an old run's JSONL can move to cold storage
  and its head row still proves what it contained.

**Why this is third.** It degrades gracefully. A slow verification is
annoying; it does not lose a payment or waste a rate-limit budget. I would
rather ship the durable queue and the circuit breaker and live with a
verification pass that has to be scoped to one run.

---

## What is deliberately not on this list

Throughput of the LLM calls. At 2 calls per episode, content-hash cached,
with the regex baseline resolving most diagnoses for free, 10k/day is not
where that breaks — `evidence/report.md` measures Rs 0.55 per 100 episodes
with the cache ignored entirely. I would rather name three things I can
defend than pad the list to five.
