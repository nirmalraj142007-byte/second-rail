# System walkthrough — recording script

This is a different document from `demo/script.md` (the timed 3:00/4:55
judge-pitch beat sheet). That one is built for a panel watching a clock.
This one is built for *you*, to say out loud while walking through your
own system in the order it actually runs. No timing budget, no pitch
language — just the pipeline, one stage at a time, with the real command
and the real output at each step.

**Everything below was checked against the actual code before being
written down** — file paths, function names, exact field lists, exact
commands. Where the code doesn't do something (there's one case below,
noted explicitly), this script says so instead of claiming it anyway.

## How to use this file

Each of the 9 beats has four parts: what's happening, what to say, what
to show, what it proves. Read the "what's happening" and "what it
proves" parts to *understand* the beat before you record — don't try to
memorize "what I should say." Say it in your own words once you
understand it; the sample phrasing is there to show you the register
(plain, first-person, present-tense), not a script to recite.

The whole walkthrough follows **one episode** end to end: `epi_00006`, a
₹7,500 UPI payment that failed with `card_declined`. It's a synthetic
record (`data/train.jsonl`), not a live customer payment — say that
plainly if asked, don't let the terminal output imply otherwise. Its
error fields aren't invented, though: `harvested_from` on this exact
record points at a real harvested Razorpay failure
(`evidence/harvested_errors.jsonl`, harvest_id `01M0Y2S99XRJW32SH8V15X49GB`)
— the failure text is real, the delivery mechanism (a live webhook) is
being played back, not re-triggered live.

---

## 1. PAYMENT FAILURE

**A. What is happening**

A customer tries to pay ₹7,500 by UPI and it fails. Razorpay's own
systems already tried to handle this in the moment — the checkout page,
retry prompts, whatever Razorpay's own flow does. That's over. The
customer has left. This is the point where nothing else happens unless
something reaches out to them again.

**B. What I should say**

"This is the episode I'm going to follow through the whole system — a
₹7,500 UPI payment, `card_declined`. It's a synthetic record I generated
for testing, but the actual failure text on it — the error code, the
description — comes from a real Razorpay test-mode failure I harvested
earlier. I'm not re-triggering a live payment failure on camera; I'm
replaying a recorded one so the same episode behaves identically every
take."

**C. What I should show**

```bash
python -c "
import json
with open('data/train.jsonl', encoding='utf-8') as f:
    for line in f:
        rec = json.loads(line)
        if rec['episode_id'] == 'epi_00006':
            print(json.dumps(rec, indent=2))
            break
"
```

**D. What it proves**

Nothing about the *system* yet — this just establishes the input: a real
error shape (`error_code=BAD_REQUEST_ERROR`, `error_reason=card_declined`,
`error_source=gateway`), a real amount (750000 paise), and the
`harvested_from` field that ties this specific synthetic record back to a
real Razorpay response rather than an invented one.

---

## 2. INGEST

**A. What is happening**

In production, Razorpay would `POST` a `payment.failed` webhook to my
endpoint. My server checks the signature, does the minimum work to stay
fast, and hands the event to a background worker that writes the episode
to the database — keyed so the same payment can't create two episodes.

**B. What I should say**

"In a live run, this starts with Razorpay POSTing a webhook to my
endpoint. The first thing that happens is signature verification —
`hmac.compare_digest`, constant-time, so a bad signature can't be timed
to guess its way past me. The endpoint does almost nothing else itself —
it hands off to a background worker so a slow database write can never
make the webhook response itself slow. That worker is what actually
writes the episode, and it dedups on `payment_id`, not on the webhook's
own event id — I picked payment_id deliberately, because Razorpay retries
webhook deliveries with a *new* event id each time, and if I'd deduped on
event id I'd have created a duplicate episode on every retry."

**D. What it proves**

That a malformed or replayed webhook can't create a duplicate episode or
sneak past signature verification, and that dedup survives event-id
churn from webhook retries — a real failure mode this project hit once
during its own build (see `BUILD_LOG.md`, the tunnel-retry wrong turn).

**C. What I should show**

Two options, honestly labeled:

- **Live path (needs `make tunnel` + `make serve` running):** trigger a
  real test-mode failure and watch `evidence/audit/*.jsonl` gain a
  `stage: "ingest"` record. This is the real code path, but it depends on
  a tunnel being up — riskier to show live (see `demo/script.md`'s R1
  entry for why this project doesn't put the tunnel on any critical
  path). Only do this if you've rehearsed it working.
- **Safe path — replay, not live:** `make failure-demo-backup`
  (`python -m scripts.failure_demo_backup`). This replays the same
  `payment.failed` fixture twice through the real `IngestService`, under
  two different event ids, and shows the second delivery get suppressed
  as a no-op. It's a smaller, self-contained proof of the exact dedup
  rule above, with no tunnel and no network needed. (This also doubles as
  beat 3 of the separate demos below — "idempotency/duplicate case" — so
  you can show it once and refer back to it.)

```bash
python -m scripts.failure_demo_backup
```

Point at the printed audit record naming `duplicate_episode_this_run` (or
equivalent dedup reason) for the second delivery, and at the fact that
only one episode row exists afterward, not two.

---

## 3. GATE / ELIGIBILITY

**A. What is happening**

Before anything gets diagnosed or acted on, seven deterministic checks
run in a fixed order. Any one of them can stop the episode right there.
None of them involve a model.

**B. What I should say**

"Before I even try to figure out *why* the payment failed, the episode
has to clear seven checks, in a fixed order: is this a duplicate, has the
payment already succeeded some other way, has the customer opted out, is
the payment too old, would contacting them blow my per-run exposure
ceiling, have I already contacted this customer too many times this
week, and is it quiet hours right now. If any one of them fails, the
episode stops there, and the reason gets written down — nothing gets
silently dropped."

**C. What I should show**

The seven checks scrolling past for `epi_00006` in the live demo output
(each one prints pass/fail), or read them directly:

```bash
grep -n "CHECK_ORDER" -A 10 src/gate/checks.py
```

For `epi_00006` specifically: it passes all seven — the thing that makes
it interesting isn't a gate failure, it's what happens *after* the gate,
which is why this is the main-thread episode.

**D. What it proves**

That eligibility is decided by fixed, auditable rules before any
diagnosis or model call happens — an episode that fails here never
reaches the LLM at all. `evidence/exceptions_sample.md` has real,
committed examples of episodes stopped at this stage with their exact
reason codes, if you want a second episode on screen that *does* fail a
check.

---

## 4. DIAGNOSE

**A. What is happening**

For an episode that clears the gate, the system tries to name *why* it
failed. It tries a plain regex match against known error patterns first.
Only if nothing matches does it ask the LLM.

**B. What I should say**

"Now it tries to classify why this failed. It doesn't go to the model
first — it runs a regex baseline against the error fields, and only if
that comes back empty does it call the LLM at all. For this episode, the
error reason is `card_declined`, which matches a known pattern directly
— so the LLM never even gets called here. On the harder cases — real,
messy production-shaped error text — regex fails a lot more often, and
that's when the model actually earns its cost."

**C. What I should show**

The live output line for this episode: `C2 (1.00) via regex` — that
`via regex` is the tell. To show the LLM-fallback path exists and is
real (not just claimed), point at `evidence/classification_metrics.json`
or `evidence/report.md` §2/§3, which report regex vs. LLM accuracy
separately, including the one place regex wins big and the model doesn't
help much (the harvested-strings result — see beat 5 below).

**D. What it proves**

That the model is a fallback for the unmatched tail, not the default
path — `src/diagnose/classifier.py`'s own design doc states this
explicitly, and the regex-first ordering is literally the first thing
that function does, checkable directly in the file.

---

## 5. ACTION SELECTION

**A. What is happening**

This is the architectural core of the whole project. The deterministic
policy engine looks at the diagnosed cause, the amount band, the
customer's segment, and the instrument, and resolves a small set of
allowed actions — at most three, "do nothing" always included. Only
*then* does the LLM get a turn, and its only job is to pick one item from
that list. It cannot add a fourth option, and it never sees the actual
rupee amount.

**B. What I should say**

"This is the part I want to be precise about. My code decides what
actions are even possible for this episode — never more than three, and
'do nothing' is always one of them — before the model ever gets
involved. The model's whole job is picking one of those. It can't invent
a new action, and it doesn't get to see the things that would let it
reason about risk directly — no rupee amount, no threshold values, no
guardrail names. It sees an amount *band* — like 'this is a mid-size
payment' — not the number. If it ever comes back with something that
isn't verbatim one of the options I gave it, the whole run stops right
there. That's not a soft warning, it halts."

**C. What I should show**

```bash
cat config/policy_table.yaml | head -40
```

— point at `admissible_actions` (max 3, `no_action` always present) and
the comment block explaining the tier convention. Then:

```bash
grep -n "LLM_VISIBLE_FEATURES" -A 8 src/choose/selector.py
```

— the exact whitelist the model is allowed to see:
`error_code, amount_band, segment, instrument, prior_contacts_7d,
hours_since_failure`. No amount. No threshold. For `epi_00006`
specifically, the live output shows `candidates: open_ticket / no_action`
— two options, both from the policy table, and the selection line
`-> open_ticket`.

**D. What it proves**

Two separate, both-real claims, and it's worth being precise about which
is which: (1) the candidate set is capped and code-constructed —
`config/policy_table.yaml` is a plain YAML file the model never writes
to; (2) the model's information is genuinely restricted — the whitelist
above is enforced by a real test
(`tests/test_choose.py` asserts the rendered prompt contains none of the
forbidden tokens), not just a comment saying it should be. The
admissibility check (the model can't invent an action) is enforced in
`ActionSelector.select()` itself and is covered next.

---

## 6. ESCALATION / FINAL SAFETY CHECK

**A. What is happening**

Two separate deterministic checks run on the model's choice before
anything is allowed to happen. First: is the chosen action actually one
of the options offered? If not, the whole run halts — this is not a
retry-and-continue failure, it stops. Second: does *this* action, for
*this* episode, get to run automatically, or does it need a human
keystroke first? That decision is also not made by the model — it comes
from the same policy table that built the candidate list.

**B. What I should say**

"After the model picks, two things check it before it's trusted. First,
my code checks that what came back is actually one of the options I
gave it — not close to one, exactly one, verbatim. If it ever isn't,
even after one retry, the entire run stops. That's the one failure mode
this project treats as unrecoverable — a wrong diagnosis is fine, the
system just says 'unknown' and moves on, but a model picking something
outside its box is not something I let it recover from. Second, separate
question: does this specific action get to run on its own, or does a
human need to approve it first? For this episode, the amount is above my
₹5,000 auto-approve line, so it needs a keystroke from me before
anything happens."

**One thing I want to be exact about, in case it comes up:** I want to
say a gate re-check runs here too, and it doesn't — the seven checks from
step 3 run exactly once, before diagnosis, not a second time after the
model picks. What genuinely runs twice-in-spirit is the *tier* decision:
the amount-vs-ceiling check happens once at the gate for bookkeeping, and
a second time, independently, inside the policy table's own rule
resolution — for this episode both agree (above ₹5,000 → human
keystroke), but they're two different mechanisms and I don't want to
claim they're the same check running twice.

**C. What I should show**

The live approval prompt panel — a real keystroke moment, not
simulated. If recording non-interactively (no tty), the same episode
queues instead: `human_keystroke - no interactive tty, queued for
'make approve'`, and `make approve` resolves it from
`demo/approval_queue.json`. Also worth having on screen once, separately
from the main episode: the admissibility halt itself doesn't have a
friendly demo (deliberately — provoking a real model into naming an
invalid action isn't something to stage), so cite it from the code
instead: `src/choose/selector.py`'s `AdmissibilityError` raise, and
`tests/test_choose.py`'s test that a bad LLM response actually halts the
run (not just logs a warning).

**D. What it proves**

That the model's own output is never trusted at face value twice: once
for shape (is this even a legal answer) and once for authority (is this
specific episode allowed to act without a human). Neither check is a
model call.

---

## 7. EXECUTE

**A. What is happening**

Only an approved action reaches this stage. If the action involves
contacting the customer, the executor calls the real Razorpay API and
creates a Payment Link — in test mode. Creating the link does not move
any money. The customer still has to open it and pay.

**B. What I should say**

"Once the action is approved, the executor creates a real Razorpay
Payment Link — this is a real API call, test mode, and I want to be
clear about what it actually does: creating this link moves zero rupees.
Nothing happens until the customer opens it and completes a payment
themselves, with a real payment method — UPI, card, netbanking. The link
can also be cancelled, and if this exact episode is ever submitted for
execution twice — say, a webhook redelivers — the second attempt doesn't
create a second link. The idempotency key is a hash of the payment id
and the policy rule that was applied, and that same key is both the
link's own reference_id on Razorpay's side and a database uniqueness
constraint on my side. Razorpay itself refuses a second link with a
reference_id it's already seen — I've confirmed that against the real
API, it's not just something I assumed from the docs."

**C. What I should show**

```bash
python -m scripts.demo --source demo/_pinned_take.jsonl --execute
```

(this is the pinned single-episode-plus-cluster set built for exactly
this walkthrough — `epi_00006` first). Approve the keystroke live. Point
at the printed `plink_id` and, side by side, the same link in the
Razorpay test-mode dashboard — same id, same amount, `status: created`.
Then, separately, show the idempotency proof:

```bash
python -m scripts.failure_demo
```

— its final lines re-run an already-executed episode under the same
idempotency key and show `duplicate_suppressed`, zero new links created.
(This script's fault-injection beat, earlier in its output, is the
"failure-injection/retry" demo further below — same command covers both.)

**D. What it proves**

That the only external effect is a cancellable link requiring the
customer's own authentication (never a debit initiated by this system),
and that idempotency is real and dual-enforced — locally (SQLite
`UNIQUE(idempotency_key)`) and confirmed against Razorpay's own live
rejection of a repeated `reference_id`, not just assumed from
documentation.

---

## 8. ATTRIBUTE / RECOVERY

**A. What is happening**

A created Payment Link is not counted as a recovery. The system has to
separately observe that the customer actually paid, and that the payment
happened within a defined time window after the link was created.
Anything else — no payment, a late payment, a payment through some
unrelated channel — does not count.

**B. What I should say**

"Creating the link isn't the finish line. Separately, the system watches
for the outcome — either a webhook telling it the link was paid, or, if
I'm not relying on the webhook path, polling Razorpay directly and
asking. A payment only counts as recovered if it happened within 48
hours of the link being created and it's actually traceable back to
*this* link — same `plink_id`, or the same order if the event doesn't
carry a link id. If the payment shows up outside that window, or through
some other channel entirely, I don't credit it as a recovery — there are
specific reason codes for exactly those cases, and they're not swept
under 'success.'"

**C. What I should show**

```bash
python -m scripts.watch --run-id <the run_id from step 7> --poll
```

Prints `mode: polling` first, then the resolved outcome per episode (or,
honestly, "nothing to attribute yet" if the link from this take hasn't
actually been paid — attribution can't be faked, so say plainly if
nothing's resolved yet). For the rule itself:

```bash
sed -n '1,30p' src/attribute/rules.py
```

— rule AR-01, printed in the file in the exact words used on screen.

**D. What it proves**

That "recovered" is a real, checked outcome — not "a link exists." The
reason-code list (`recovered_within_window`, `outside_attribution_window`,
`unattributable_recovery`, `partial_payment_not_attributed`) is what
makes the eventual recovery number in `evidence/report.md` non-circular:
a link that's never paid, or paid too late, or paid through a different
channel, does not silently become a win.

---

## 9. AUDIT

**A. What is happening**

Every decision at every stage above — every gate check, the diagnosis,
the chosen action, the approval, the execution, the attribution outcome
— gets written as one record in an append-only log. Each record is
cryptographically linked to the one before it, so an old record can't be
edited without breaking the chain.

**B. What I should say**

"Every one of the steps I just walked through writes its own record to
an append-only log, and each record includes the hash of the record
before it. That means the records aren't just a list — they're a chain.
If I went back and edited or deleted anything, even one old record, the
hash of the record after it would no longer match, and verification
would catch it at that exact point. I can prove that live — not just
claim it."

**C. What I should show**

```bash
python -m src.audit.verify --all
```

— prints `chain intact - N records (Xs)`. Then the tamper proof:

```bash
python -m src.audit.verify --tamper-test
```

— flips one byte in a *throwaway copy* of the newest audit file (never
the real one) and verification correctly reports `chain BROKEN at seq N
- expected sha256:... got sha256:...`.

**D. What it proves**

That the audit trail is genuinely tamper-evident, not just append-only
by convention — the tamper test is a real bit-flip against a real hash
chain, and the failure it produces names the exact record where the
chain breaks.

---

# Separate short demos

Run these after the main walkthrough, each as its own beat. For each:
state what you're testing, what you expect, run it, then say what
actually happened and why it matters — in that order, live, not
pre-narrated.

## 1. A blocked payment (hard refuse — individual)

**Testing:** that a customer who opted out never gets contacted, no
matter what else is true about the episode.

**Command:**
```bash
python -c "
import json
with open('data/train.jsonl', encoding='utf-8') as f:
    lines = [line for line in f if json.loads(line)['episode_id'] == 'epi_00005']
open('demo/_blocked_demo.jsonl', 'w', encoding='utf-8').writelines(lines)
"
python -m scripts.demo --source demo/_blocked_demo.jsonl
```

**Expect:** the `opt_out` check to fail and the episode to stop there —
no diagnosis, no candidates, no link.

**What it proves:** opt-out is checked before anything else about the
episode matters, including a real, harvested-style failure reason
(`gateway_technical_error`) that would otherwise look actionable.

## 2. An anomalous/failure-burst situation (cluster refusal)

**Testing:** that a burst of episodes sharing one cause in a short window
gets treated as a systemic issue, not 40 individual actions.

**Command:** the tail of the main walkthrough's own run — `epi_00006`
followed by the pinned 40-episode `gateway_technical_error` cluster
(`epi_00008`–`epi_00047`) is already in `demo/_pinned_take.jsonl`, so
this needs no new command — it's what the main take's run naturally
reaches right after the approval beat.

**Expect:** the run to process all 40 individually (each fails its
gate check for the same reason), then collapse to one aggregate refusal
line and stop the run.

**What it proves:** the cluster threshold (`outage_cluster_threshold: 15`
in `config/guardrails.yaml`) is a real, swept number — see
`experiments/thresholds/outage_cluster.md` — and the response to a
detected outage is "stop and say why," not 40 separate contact attempts.

## 3. An idempotency/duplicate case

**Testing:** that redelivering the same webhook event doesn't create a
second episode.

**Command:**
```bash
python -m scripts.failure_demo_backup
```

**Expect:** two deliveries in, one episode out; the second delivery's
audit record names the dedup reason explicitly.

**What it proves:** dedup is keyed on `payment_id`, survives a changed
event id on redelivery — the exact failure mode this project hit for
real during its own build (see `BUILD_LOG.md`'s tunnel-retry entry).

## 4. A failure-injection/retry case

**Testing:** real API backoff behavior under a real, injected 429, and
that the retry cap actually stops rather than hammering.

**Command:**
```bash
python -m scripts.failure_demo
```

**Expect:** episodes 1 through 6 (or so) create real links normally;
episode 7 gets a real injected 429, backs off at 1s/2s/4s (matching
`executor_retry_cap: 3` in `config/guardrails.yaml`), then gives up and
is recorded as `execution_failed` rather than retried forever; the batch
continues past it; the run finishes by re-running the failed episode and
proving the idempotency key still blocks a duplicate; every link the run
created gets cancelled at the end.

**What it proves:** backoff and the retry cap are real, not simulated —
this hits the live Razorpay API. **Caveat worth saying out loud:** this
script also occasionally hits *real*, unrelated rate-limiting from
sustained testing — if more than just episode 7 shows a 429, say so
plainly rather than pretending only the intended fault fired; the system
handling that gracefully is itself part of the honest story. If the rig
misfires badly, the stated fallback is
`python -m scripts.failure_demo_backup` (demo 3 above) — say on camera
that you're switching to it and why.

## 5. Audit-chain verification

Covered in step 9 above (`python -m src.audit.verify --all` then
`--tamper-test`) — repeat it here standalone if you want it as its own
beat rather than folded into the main walkthrough.

## 6. Rollback

**Testing:** that every link a run created can be cancelled in one
command, by run id.

**Command:** using the run id printed by the main walkthrough's own
`make demo` run (step 7):
```bash
python -m src.execute.rollback --run-id <run_id>
```

**Expect:** a per-link result table, `cancelled N/N`, or an honest
`no links created` if the approval in step 7 wasn't actually granted
live.

**What it proves:** every real Payment Link this project's own demo
activity creates can be undone in one command — not just in principle,
a real cancel call against the real API, confirmed against the
dashboard.

---

# Natural speaking version (no memorizing required)

Say this cold, in this order, filling in your own words at each `//`:

> "This is a payment recovery system. A customer's payment failed —
> here's the actual episode, ₹7,500, `card_declined`. // Normally
> Razorpay sends me a webhook about this; I verify it and dedup it so
> the same failure can't create two records. // Before I do anything
> about it, seven checks run — has this customer opted out, is the
> payment too old, that kind of thing. This episode passes all of them.
> // Now I try to figure out why it failed. I use plain pattern matching
> first — regex — and only fall back to an AI model if that doesn't
> match. This one matched, so no model call happened here. // This next
> part is the important architectural point. My code decides what
> actions are even possible — at most three, and 'do nothing' is always
> one of them — before the model does anything. The model only picks
> from that list. It never sees the actual rupee amount, only a band.
> // Once it picks, two more checks run, and neither of them is the
> model: is the pick actually a legal option — if not, the whole run
> stops — and does this specific episode need a human to approve it.
> This one's above my ₹5,000 line, so it needs my keystroke. // I
> approve it, and now the executor creates a real Razorpay Payment
> Link — test mode. This does not move money. The customer still has to
> open it and pay. And if this exact episode ever came through twice,
> the second attempt wouldn't create a second link — I can show that.
> // The system doesn't count this as recovered just because a link
> exists — it watches for an actual payment, within a time window, and
> only then counts it. // And every one of these steps I just described
> wrote a record to a hash chain — I can prove right now that nothing in
> that chain has been altered."

---

# "Judge may ask" — likely questions, short honest answers

**Q: Does the AI ever decide how much money to move, or whether to move
it at all?**
A: No. It picks one action from a list my code builds, and even that
pick is checked before it's trusted. The only external effect anything
in this system produces is a cancellable Payment Link requiring the
customer's own authentication — no code path debits anyone.

**Q: What happens if the model returns something outside the allowed
actions?**
A: `ActionSelector.select()` validates the raw response against the
admissible set itself, not just the API's schema. If it's not a verbatim
match, even after one repair retry, `AdmissibilityError` halts the
entire run. That's deliberately not a soft failure.

**Q: Does the AI see the payment amount?**
A: No — it sees `amount_band` (a bucket like "mid-value"), never the raw
rupee figure. The whitelist is six fields:
`error_code, amount_band, segment, instrument, prior_contacts_7d,
hours_since_failure`. Enforced by a real test that checks the rendered
prompt, not just documented as a rule.

**Q: Is the gate re-checked after the model picks an action, before
execution?**
A: The seven eligibility checks run exactly once, before diagnosis —
not a second time after the model's pick. What does run after the
pick is a separate, different check: whether *this* action needs
human approval, resolved from the same policy table. I want to be
precise that these are two different mechanisms, not the same gate
running twice.

**Q: Does creating a Payment Link mean the payment succeeded?**
A: No. It means a cancellable link exists. The system separately watches
for an actual payment event and only counts it as recovered if it
arrives within the attribution window and is traceable back to that
specific link.

**Q: How do you know the audit log hasn't been tampered with?**
A: Each record's hash is computed over the previous record's hash plus
its own content. `python -m src.audit.verify --tamper-test` flips one
byte in a throwaway copy and shows verification catch it at the exact
broken record.

**Q: Is any of this tested against the real Razorpay API, or is it all
simulated?**
A: Both, and I keep them labeled separately. `make demo --execute` and
`make failure-demo` are real API calls, test mode. `make eval` runs
entirely offline against a sealed, checksummed dataset with no network
call at all — that's the reproducible evidence a judge can run with no
key. The recovery *rupee figure* specifically is a simulated design
target under stated assumptions, and I say that directly rather than
present it as a measurement.

**Q: What's the honest weak point in your results?**
A: On real, raw error strings harvested from actual Razorpay test
failures — not my own synthetic data — my classifier's accuracy drops to
20%. It still beats plain regex (5%), but both numbers are weak, and
that's the harder, more honest data point, not the flattering synthetic
one.
