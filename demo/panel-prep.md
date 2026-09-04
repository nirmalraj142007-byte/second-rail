# Panel prep — five unscripted answers, plus one for the close

CLAUDE.md's voice discipline applies here more than anywhere else: these
are delivered as recall, not recital. Each heading below is
`[WRITE THIS YOURSELF]` on purpose — a rehearsed paragraph reads worse on
camera than a rough one in your own words. What follows each heading is
the real, already-in-repo evidence to draw the answer from, printed so you
know what you're standing on, not what to say.

---

## A design decision I made and then reversed, and what the losing option was

[WRITE THIS YOURSELF]

**Supporting evidence — today's rehearsal (BUILD_LOG.md, "D10 (rehearsal)
— 4 Sep 2026", first section):**

Verbatim from that entry:

```
The obvious design was one plain "auto tier" episode to open the
stream, then the >Rs 5,000 approval episode, then the outage cluster.
Checked before committing to it, not assumed: a real
PolicyEngine.resolve() pass ... showed only 4 of the 108 gate-eligible
train episodes match one of the 27 explicit policy rules at auto tier
at all -- everything else falls to default_rule, which is
human_keystroke unconditionally, regardless of amount. ... The two
candidates that do sort early -- epi_00001 and epi_00006 -- both
resolve human_keystroke, one of them (epi_00001, Rs 2,875, under the
ceiling) for a reason that has nothing to do with the rupee threshold
the beat is supposed to demonstrate. ... demo/episode_order.json uses
one episode (epi_00006) as both the drill-down and the approval beat.
```

The losing option (two episodes, one of them a false "auto" example) is
still visible in `demo/episode_order.json`'s own `_opener_note` field, and
in `git log -p -- demo/episode_order.json` if you want the literal diff.

---

## Three caps-table numbers, each justified by an experiment

[WRITE THIS YOURSELF]

**Supporting evidence — `config/guardrails.yaml`'s own inline comments,
each pointing at a real swept experiment, not a guess:**

```
auto_approve_ceiling_paise: 500000  # Rs 5,000 — queue 8.5% of 400 train
  episodes, exposure Rs 106,526/run; Rs 2,000 already pushes the queue to
  24.8%; see experiments/thresholds/auto_approve.md

outage_cluster_threshold: 15        # catches the full 40-episode planted
  outage with 0% false escalations on the train split; threshold=40
  (matching cluster size) misses it entirely — see
  experiments/thresholds/outage_cluster.md

executor_retry_cap: 3               # recovers 5/15 vs 10/15 at cap=10,
  for 20 fewer wasted API calls and ~28s less modeled wall-clock per
  still-abandoned episode; see experiments/thresholds/retry_cap.md
```

Each experiment file has its own chart in `experiments/thresholds/charts/`
(`auto_approve.png`, `outage_cluster.png`, `retry_cap.png`) — beat 8 of
`demo/script.md` puts the first one on screen. `batch_contact_ceiling`
and `executor_backoff_seconds` are explicitly flagged as *not*
experimentally derived in the same file, if you want a fourth number that
demonstrates you know the difference.

---

## One finding that shrinks my own claim

[WRITE THIS YOURSELF]

**Supporting evidence — `evidence/report.md` §3 ("Where the claim gets
weakest"), verbatim:**

> On harvested strings (raw, real fields) (n=20) — the hardest, most
> externally-anchored data anywhere in this evaluation — classifier
> accuracy collapses to 20.0%. The LLM still beats the regex baseline
> there (5.0%), but that comparison is not the finding worth taking
> seriously here — both are weak. The humbling number is the 20.0%
> itself, not which method produced it.
>
> Separately ... regex and the LLM tied across the top 5 error families by
> volume ... regex 100.0% / LLM 100.0% on all five.

Re-verified this session, not just re-read: reran all 20 harvested records
against the *current* `RegexBaseline` directly — still 19/20 unmatched
(5.0%), no drift since the committed figure. See BUILD_LOG.md's D10 entry,
R2.

---

## A real, unplanned failure from the build and my wrong first hypothesis

[WRITE THIS YOURSELF]

**Supporting evidence — pick one from BUILD_LOG.md's "Wrong turns" index
at the top of the file** (ten entries, one per dated session, each linked
to its full writeup). A fresh one from today's rehearsal if you want the
most recent:

> assumed `scripts/failure_demo.py`'s own throwaway-DB reset was enough to
> make three consecutive real runs identical. It resets the local
> database; it does not reset what Razorpay itself remembers about the
> `reference_id`s that database produces, and the second real run failed
> almost entirely on HTTP 400 "already exists" instead of the intended
> fault-injection flow.

Full writeup: BUILD_LOG.md, "D10 (rehearsal) — 4 Sep 2026", "Bug 3 (the
wrong-turn)". Confirmed against the real API before writing any fix
(create a link, cancel it, re-create with the same `reference_id` —
HTTP 200), not assumed.

---

## Where I deliberately kept the model out, and why

[WRITE THIS YOURSELF]

**Supporting evidence — `docs/where-the-llm-is-not.md`, opening lines:**

> I call an LLM twice per episode, at most: once to classify a cause when
> the regex baseline doesn't match, once to pick one action from an
> already-narrowed set of at most three. ... Everywhere else in this
> system the model is refused outright — not "discouraged," refused, with
> a test that greps the package for the client symbol and fails the build
> if it's there (`test_llm_boundary.py`).

The document lists every package the refusal covers
(`src/gate/`, `src/execute/`, `src/attribute/`, `src/audit/`,
`src/ingest/`, `src/db/`) and the specific decision inside each one that
never reaches the model — thresholds, idempotency, the hash chain,
attribution windows. `demo/script.md`'s beat 7 ("The boundary") is the
`config/policy_table.yaml` + `config/guardrails.yaml` files on screen;
this heading is the verbal complement to that beat, not a repeat of it.

---

## What does a Payment Link plus a cron job not do?

[WRITE THIS YOURSELF]

**Supporting evidence — `evidence/report.md` §4, the `FIXED_RETRY_AT_T30`
baseline (Runner's gate-only fallback: every gate-eligible episode gets
`placeholder_action`/`P-00`, unconditionally — no diagnosis, no policy
constraint, no admissible-set check):**

Verbatim from `evidence/report.md` §4 — a design-target range under
stated assumptions, not a point estimate, per that section's own framing:

```
FIXED_RETRY_AT_T30 baseline -- 102/102 gate-eligible episodes
contacted, out of the 200-episode sealed batch.

gross Rs 51,427 - Rs 95,464 | false-positive cost Rs 11 - Rs 20
(1 contact(s)) | NET Rs 51,412 - Rs 95,449
```

Compared against **Second Rail** on the same batch, same run, same
section, same framing:

```
99/108 gate-eligible episodes contacted ...
gross Rs 51,497 - Rs 95,595 | false-positive cost Rs 11 - Rs 20
(1 contact(s)) | NET Rs 51,482 - Rs 95,580
```

The two net ranges overlap almost entirely — this is your ammunition for
naming what the diagnosis-and-policy layer is actually buying (9 fewer
contacts, the same false-positive count, a near-identical net range) and,
just as honestly, what it is *not* buying on this batch: a fixed
unconditional retry recovers nearly the same money. `evidence/report.md`
§4 also states directly that this comparison is a simulator output —
"this sweep perturbs my own parameters ... it is disclosure, not
evidence" — say that limitation out loud rather than let the two numbers
stand alone.
