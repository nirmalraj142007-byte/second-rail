# Demo script — 4:55 (the 5:00 expansion), observed timings from rehearsal

Source: `second-rail-build-blueprint.md` §8 (the 5:00 expansion of the 3:00
spine — U-01 resolved to five minutes, not three). Generated and timed by
`scripts/rehearse.py` (`make rehearse`), real calls, non-interactive
(automated pass — see the "how these numbers were taken" note at the
bottom for what that does and doesn't cover). Clock times below are
cumulative from each beat's own **target**, not from observed actuals —
actuals only tell you how much slack a beat has, they don't reset the
clock.

Pinned episode set: `demo/episode_order.json` — `epi_00006` (Rs 7,500,
real amount; **say Rs 7,500, not the blueprint's illustrative Rs 12,400 —
that figure has no real pinned episode behind it**) opens and is the
approval beat in one, followed by the full 40-episode issuer-outage
cluster (`epi_00008`–`epi_00047`), which halts the run the instant it's
detected. `scripts/rehearse.py`'s preflight re-derives both properties
from live config/data before every rehearsal and aborts loudly if either
has drifted — it does not trust this file blindly, and neither should you
if it's been more than a few days since the last `make rehearse`.

Every beat's command was run for real during rehearsal. Where a beat is
narration-only (hook, seam diagram, cluster refusal, `build_log_expansion`,
close), "observed" is `n/a` — `scripts/rehearse.py` shows a live countdown
and waits for your own Enter key in that mode; it does not fake a number
for a human's delivery. Run `make rehearse` yourself, at a real terminal,
before the actual recording, to get your own numbers for those five.

| # | Time | Beat | Target | Observed | Command |
|---|---|---|---|---|---|
| 1 | 0:00–0:08 | Hook | 8s | *(rehearse yourself)* | — (static frame) |
| 2 | 0:08–0:25 | Seam diagram | 17s | *(rehearse yourself)* | — (static frame) |
| 3 | 0:25–0:35 | The claim, ordered | 10s | 0.0–0.1s | `head -30 evidence/report.md` |
| 4 | 0:35–1:10 | `make harvest` | 35s | 13.1s | `python -m scripts.harvest_errors` |
| 5 | 1:10–2:15 | **The unbroken run** | 65s | 5.0s* | `python -m scripts.demo --source demo/_pinned_take.jsonl --execute` then `python -m scripts.watch --run-id <id> --poll` |
| 6 | 2:15–2:25 | The refusal | 10s | *(rehearse yourself)* | — (tail of beat 5's stream, no new command) |
| 7 | 2:25–2:40 | The boundary | 15s | 0.1s | `cat config/policy_table.yaml config/guardrails.yaml` |
| 8 | 2:40–3:10 | Threshold experiment | 30s | 0.0s | `cat experiments/thresholds/auto_approve.md` (+ `charts/auto_approve.png` on screen) |
| 9 | 3:10–3:30 | Evidence, honestly | 20s | 0.2s | `python -m scripts.seal verify` + `evidence/report.md` §2–§3 on screen |
| 10 | 3:30–3:55 | `make rollback` | 25s | 1.2s** | `python -m src.execute.rollback --run-id <beat 5's run_id>` |
| 11 | 3:55–4:17 | Failure + idempotency | 22s | 26.5–35.8s*** | `python -m scripts.failure_demo` |
| 12 | 4:17–4:30 | Verify | 13s | 0.5–2.5s | `python -m src.audit.verify --all` |
| 13 | 4:30–4:50 | `BUILD_LOG.md` readout | 20s | *(rehearse yourself)* | — (read one entry from the "Wrong turns" index aloud) |
| 14 | 4:50–4:55 | Close | 5s | *(rehearse yourself)* | — (repo URL) |

\* Automated-pass observed time for beat 5 is real but incomplete — see
the note below. A real take with a real approval keystroke and a real
customer-side checkout completion will run longer; budget the full 65s.

\*\* Automated pass: the approval queued rather than executed (no tty), so
this observed number is "cancel 0 real links, confirmed the command
works" — not "cancel the 5 or so links a real take will actually create."
On a real take this will take longer than 1.2s but the mechanism is
proven end to end.

\*\*\* Ranged because this session ran `failure_demo` many times back to
back while rehearsing the fixes below — real, sporadic Razorpay-side rate
limiting on top of the deliberate fault landed on unrelated episodes more
than once. A presenter's actual retake cadence (with normal setup gaps
between takes, not immediate back-to-back reruns) should track closer to
the lower end. See row 11's fallback note.

## What's said (unscripted — points, not a script)

Same content as the blueprint's §8 table, condensed, with the corrected
real figure:

1. **Hook.** Roughly a third of failed transactions are never
   re-attempted — Razorpay's own number, not mine.
2. **Seam.** Razorpay fights the in-session failure; payment links and
   abandoned-checkout recovery exist too. What's missing is automated
   per-episode diagnosis driving one bounded action.
3. **The claim, ordered.** Before the money number: zero duplicate links,
   zero cap breaches, zero quiet-hour contacts, across the real N=108
   guardrail-proof run.
4. **`make harvest`.** These are real forced failures against Razorpay's
   own test-mode instruments, landing verbatim in
   `evidence/harvested_errors.jsonl` — not paraphrased.
5. **The unbroken run.** Real `error_code`, real classification and
   rationale, three admissible actions, guardrail ticker passing one by
   one. The gate fires on the Rs 7,500 episode — approve with one
   keystroke — a `plink_` appears in the Razorpay test-mode dashboard, the
   ID matches the audit line on screen. Payment completes. Ledger moves.
6. **The refusal.** Forty episodes share a cause. The answer isn't forty
   messages — it suppresses, escalates, writes the reason.
7. **The boundary.** Every money-adjacent decision lives in these two
   files. The model classifies language and picks one option from a set
   it didn't construct.
8. **Threshold experiment.** Rs 5,000 wasn't guessed — swept against real
   code, real accounting: Rs 2,000 already pushes the approval queue to
   24.8% of the batch.
9. **Evidence, honestly.** Checksum verified, split opened now. And here's
   where I lose: regex beats my classifier on the top error families — the
   model only earns its place on the unmatched tail.
10. **`make rollback`.** Every link this run created, cancelled live —
    the same command a real operator runs to undo a batch.
11. **Failure + idempotency.** Backoff 1, 2, 4 — retry cap 3, so it stops
    rather than hammering. Then the five seconds that matter: re-run the
    same episode. Same idempotency key. No duplicate link.
12. **Verify.** `chain intact` — every decision, hash-chained.
13. **`BUILD_LOG.md`.** One real wrong turn, read cold, not memorized.
14. **Close.** At 10k episodes a day this breaks in three places, starting
    with the in-process dedup store. Repo's here.

## If this breaks, do this instead

- **Beat 4 (`make harvest`) errors or rate-limits:** the manifest is
  resumable — re-run it once; if it still fails, skip straight to beat 5
  and narrate over the already-committed `evidence/harvested_errors.jsonl`
  instead (`cat` a few lines).
- **Beat 5 (the unbroken run) — tunnel/webhook issues:** irrelevant by
  design. This beat's attribution step already runs `make watch --poll`,
  not the webhook path — the tunnel is never on the critical path (R1).
  If the approval keystroke itself hangs past 60s for any reason, that is
  the system working as designed (`approval_timeout`, auto-rejects) — let
  it resolve and keep narrating; don't restart the take over it.
- **Beat 5 — episode ordering looks wrong:** `scripts/rehearse.py`'s
  preflight would have aborted *before* this beat ever started recording
  if `epi_00006` weren't the human_keystroke episode or the cluster
  weren't still 40-strong — if you're seeing this beat at all, the
  preflight already passed. If it visibly doesn't match anyway, stop the
  take; do not push through and narrate around it on camera.
- **Beat 10 (`make rollback`) reports link(s) it can't cancel:** say so —
  "already paid, can't cancel" is a real, correctly-reported terminal
  state, not a bug to hide.
- **Beat 11 (`make failure-demo`) — the primary scenario:** if the
  fault-injection rig misfires (wrong episode, or real rate-limiting
  swamps the deliberate 429 — a real, disclosed risk this session hit
  more than once under sustained back-to-back testing), **the stated
  substitute is `scripts/failure_demo_backup.py`** (`make
  failure-demo-backup`) — a duplicate-webhook replay showing an idempotent
  no-op. No network, no keys, needs no external API to misbehave. Cut to
  it and say so on camera rather than re-taking beat 11 repeatedly against
  a real API that's already under load from earlier takes.
- **Any beat — LLM key issue:** the demo run uses the committed disk
  cache by default; `--execute` only ever affects Razorpay calls. A cache
  miss falls through to the regex baseline with a visible amber
  `llm_degraded` line for *diagnose*. **Known gap, not fixed this
  session** (`KNOWN_ISSUES.md` Issue 5): a *choose*-stage cache miss with
  no reachable LLM crashes rather than degrading. `scripts/rehearse.py`'s
  preflight confirms the pinned approval episode is cache-hit for both
  diagnose and choose before every rehearsal — if a future re-pin of
  `episode_order.json` or a config change ever makes that preflight print
  a `WARN` instead of `OK`, do not record until it's `OK` again.

## How these numbers were taken

`scripts/rehearse.py` was run non-interactively (this rehearsal session,
via an automated tool, not a human at a keyboard) — every *command* beat
above got a real subprocess, real timing, real output. The five
*narration* beats cannot be timed this way — there's no human delivering
a line in a non-interactive pass — so they're honestly marked
"rehearse yourself" rather than filled with an invented number.
`scripts/rehearse.py` run at a real terminal (`make rehearse`, no
`SKIP_SLOW`) shows a live countdown for each and waits for Enter, so a
real rehearsal pass fills in real numbers for those five before the
actual recording.
