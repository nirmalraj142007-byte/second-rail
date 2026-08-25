# Second Rail — Build Blueprint v1.0
**Issued:** 25 Aug 2026 (D1) · **Freeze:** 2 Sep (D9) · **Submit:** 4 Sep (D11) · **Close:** 5 Sep
**Sources:** `razorpay-buildathon-proposal.md` (scope) + `judge-expectations-second-rail.md` (quality bar). Both binding.

**Standing rule for this document:** the judge file states that no numeric rubric exists publicly and that planning against inferred weights ("20 points for evidence, 5 for originality") is a mistake. This blueprint therefore maps every decision to **judge clauses**, not to the proposal's Step-1 weight table. The proposal's weights survive only as a priority heuristic, never as a claim.

---

## 1. Project Thesis

When a payment fails while the customer is still on the checkout page, Razorpay already fights for it — Optimizer reroutes, Intelligent Payment Retry runs a next-best-action. The moment the tab closes, that fight stops, and the payment joins a pile nobody re-attempts. Razorpay's own Optimizer material puts that pile at roughly a third of all failed transactions. **Second Rail is the automated recovery desk for that pile.** It ingests `payment.failed`, gates the episode against a deterministic eligibility check, diagnoses the real cause from the issuer's own error strings, picks exactly one action from a pre-registered admissible set, gets a human keystroke when the money gets big, executes it as a *cancellable Razorpay Payment Link the customer must authenticate themselves* — and then reports, across a 200-episode sealed batch, what actually came back, net of what it cost to be wrong. It is built for a merchant ops team that currently has no owner for post-session failures, and it is deliberately the boring version: no code path in Second Rail moves money, every money-adjacent threshold sits in a YAML file a panelist can read in two minutes, and the model is kept away from every decision that touches rupees. It is not "an AI agent that recovers revenue." It is the diagnosis-to-bounded-action loop for post-session failures, and the honest accounting that goes with it.

**Positioning discipline (judge §10):** do not say "nobody owns the payment after the session ends." Razorpay ships payment links and abandoned-checkout recovery; merchants run retry emails. The seam Second Rail owns is *automated per-episode diagnosis driving a bounded, gated, audited action* — not the category.

---

## 2. Requirements Ledger

Source key: **P§x** = proposal section · **J§y** = judge section · **Both**.
Type: **F** functional · **NF** non-functional · **DC** demo-critical.
P0 = the demo or the submission is broken without it.

### A. Hard gates (fail = nothing else counts)

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| G-01 | Candidate is a currently enrolled student; evidence available on request | J§0 | NF | P0 |
| G-02 | Candidate can be physically in Bangalore from Sept for 6 or 12 months | J§0 | NF | P0 |
| G-03 | Razorpay test-mode keys (`rzp_test_*`) present in `.env` and a successful `orders.create` round-trip logged, on D1 | Both (P§9 D1, J§0) | F | P0 |
| G-04 | Official track page re-read directly from razorpay.com/buildathon on D1; a dated line in `BUILD_LOG.md` records it | J§0 | NF | P0 |
| G-05 | Zero real PII anywhere in repo, dataset, or git history; `data/generator.py` committed and seeded | Both (P§14, J§10) | NF | P0 |
| G-06 | Zero secrets in git history. `.env.example` only. Verified by `git log -p \| grep -E 'rzp_(live\|test)_'` returning nothing | Both (P§11, J§7/§10) | NF | P0 |
| G-07 | No card PAN is stored, logged, or rendered anywhere. README states this as a heading | Both | NF | P0 |
| G-08 | `outcome_model.md` committed with a git timestamp **strictly earlier** than the first eval run commit. Verified by `git log --format='%ad %s' --date=short` | J§3, P§9 | NF | P0 |

### B. Core loop — functional

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| C-01 | `payment.failed` webhook received over HTTPS, HMAC-SHA256 signature verified against `X-Razorpay-Signature`, rejected with 400 on mismatch | P§3 | F | P0 |
| C-02 | Webhook replay is a no-op at the ingest boundary: same `payment_id` twice → one episode, one audit record with `outcome:"suppressed", reason:"dedup_replay"` | Both (P§3, J§4) | F | P0 |
| C-03 | Out-of-order delivery handled: a `payment.captured` arriving before its `payment.failed` does not create a recovery action | J§4 | F | P0 |
| C-04 | Deterministic eligibility gate evaluates 7 named checks in fixed order: episode age ≤72h, amount ≤ per-run ceiling, customer opt-out, already-paid-elsewhere, duplicate webhook, contact frequency cap, quiet hours. Each writes pass/fail + reason | P§3, J§1c | F | P0 |
| C-05 | No episode is ever silently dropped. Suppressed episodes appear in the exception list with a machine-readable reason. `count(episodes) == count(actioned) + count(suppressed) + count(execution_failed)` asserted in the eval | Both | F | P0 |
| C-06 | Cause classifier assigns exactly one canonical class + confidence [0,1] + one-line rationale, from `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` + context features | P§3 | F | P0 |
| C-07 | Taxonomy is a data file (`config/taxonomy.yaml`), not code, and each class carries ≥1 **harvested real error string** as its anchor example | J§3 [W][HARD] | F | P0 |
| C-08 | Policy engine maps (cause × amount band × segment × instrument) → admissible action set of ≤3, deterministically, from `config/policy_table.yaml`. Same input → same set, asserted by a unit test | P§3 | F | P0 |
| C-09 | Agent's chosen action is inside the admissible set 100% of the time, or the run halts. Admissibility rate reported | Both (P§6, J§3) | F | P0 |
| C-10 | Escalation is **tiered, not binary**: auto-approve band, human-keystroke band, hard-refusal band — each with a distinct named reason in the audit log | J§1c [W] | F | P0 |
| C-11 | Guardrail engine re-checks caps, DND, quiet hours and idempotency *after* action selection and *before* execution. A cap breach aborts the episode, not the run | P§4 | F | P0 |
| C-12 | Executor creates exactly one Razorpay Payment Link per approved episode, in test mode, with `expire_by` set and `reference_id` derived from a stable key | Both | F | P0 |
| C-13 | Outcome listener consumes `payment_link.paid` / `payment.captured` / `payment_link.expired` and attributes recovery inside a declared attribution window (see U-14 — window length currently undefined) | P§3 | F | P0 |
| C-14 | Attribution rule is stated on screen and in `report.md`, not just implemented | J§1b | F | P0 |
| C-15 | LLM drafts recovery copy (English) with tone matched to segment | P§4 | F | P2 |
| C-16 | Hinglish copy generation | P§5 (first cuts) | F | P2 — **CUT** |

### C. Engineering instincts (read from `src/`, not the video)

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| E-01 | Idempotency key on every external call, derived from a stable identifier (`payment_id` + `policy_rule_id`), **not** the webhook event id | J§4 [F][HARD] | F | P0 |
| E-02 | Re-running an executed episode produces a key match and **zero** new links. Provable on camera in ≤5s | Both (P§8, J§4/§8) | DC | P0 |
| E-03 | Local uniqueness enforced by a SQLite `UNIQUE` index on `(payment_id, policy_rule_id)`; server-side uniqueness enforced by Razorpay's rejection of a duplicate `reference_id`. Both paths tested | Derived (J§4) | F | P0 |
| E-04 | The only external effect the system can produce is reversible: a cancellable Payment Link with expiry requiring customer authentication. **No code path moves money.** Stated as a README heading | J§4 [F][HARD], P§4 | NF | P0 |
| E-05 | `make rollback RUN_ID=X` cancels every link that run created, prints a per-link result table, and writes a `rollback` audit record. Demonstrated | Both (P§7, J§4) | DC | P0 |
| E-06 | Default mode is `--dry-run`. Execution requires an explicit `--execute` flag. Asserted by a test that runs the CLI with no flag and checks zero HTTP POSTs | Both | F | P0 |
| E-07 | Every money-adjacent threshold lives in `config/guardrails.yaml`. `grep -rn '5000\|₹' src/` returns no thresholds | Both (P§7, J§4) | NF | P0 |
| E-08 | **Every threshold in the caps table has a one-line justification AND a reproducible experiment behind it.** Minimum three: auto-approve ceiling, outage-cluster size, retry cap | J§4 [W], J§12 | NF | P0 |
| E-09 | `docs/where-the-llm-is-not.md` exists and enumerates every decision refused to the model | Both (P§13, J§4 [W]) | NF | P0 |
| E-10 | Backoff is hand-rolled (~40 lines) rather than `tenacity`, so retry attempt number and delay are written into the audit record | Derived | NF | P1 |
| E-11 | Stated scaling-failure analysis: three places this breaks at 10k episodes/day, in fix order, each defensible for five minutes | Both (P§13, J§4 [W]) | NF | P1 |
| E-12 | Stopping rules: ≥3, in config, each demonstrably firing — consecutive executor errors (3), cap breach, kill-switch file. Plus shared-cause cluster >15 → escalate | Both (P§7, J§1d) | F | P0 |
| E-13 | Kill switch is a file on disk; its presence halts the run within one episode and writes a reason | P§7 | F | P1 |

### D. Audit

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| A-01 | Append-only JSONL, one record per decision stage, containing inputs hash, chosen action, `policy_rule_id`, guardrail check results, outcome | J§1e [F] | F | P0 |
| A-02 | Records hash-chained: `hash = sha256(prev_hash + canonical_json(record))` | Both | F | P0 |
| A-03 | `make verify-audit` walks the chain and prints `chain intact — N records` in **under 2 seconds** | J§1e [W] | DC | P0 |
| A-04 | Tampering with any record causes `verify-audit` to fail loudly and name the first broken index. Tested | Derived | F | P1 |
| A-05 | `evidence/audit_sample.jsonl` committed | P§11 | NF | P1 |

### E. Evidence & metrics

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| M-01 | ≥200 episodes evaluated; batch size printed on screen | J§1b [F] | DC | P0 |
| M-02 | Recovery reported against **at least one** baseline, named on screen | J§1b [F] | DC | P0 |
| M-03 | Recovery figure **never** displayed as a bare point estimate — range or nothing, everywhere it appears | J§3 [F][HARD] | DC | P0 |
| M-04 | Non-circular metrics lead the report and the video: (1) guardrail correctness under fault injection, (2) action admissibility rate, (3) throughput + LLM cost per 100 episodes in ₹ | J§3 [F] | DC | P0 |
| M-05 | Recovery range framed explicitly as a **design target under stated assumptions**, third in order | J§3 | DC | P0 |
| M-06 | Guardrail correctness measured over **N ≥ 200 real test-mode link creations under injected faults**: 0 duplicate links, 0 cap breaches, 0 quiet-hour contacts, N/200 idempotency collisions correctly detected | Both (P Step5 #1, J§3) | DC | P0 |
| M-07 | False-positive cost as a **rupee figure netting against gross**. `net = gross − fp_cost`. Net is the reported number | Both (P§6, J§3) | DC | P0 |
| M-08 | ≥1 label or input **not authored by the builder**: real `error_code`/`error_description`/`error_reason` strings harvested from forced test-mode failures, committed to `evidence/harvested_errors.jsonl` | J§3 [W][HARD], J§12 #1 | DC | P0 |
| M-09 | Classifier additionally evaluated against Razorpay's published error-code documentation as an external label source | J§3 [W] | F | P1 |
| M-10 | The 200 sealed episodes are described accurately. Either a genuine shift is introduced (unseen issuer family + unseen error strings) or the phrase "held-out test set" is replaced with "sealed split" everywhere | J§3 [W] | NF | P0 |
| M-11 | Sealed with a committed checksum; opened on camera | Both | DC | P0 |
| M-12 | **≥1 reported result that shrinks the builder's own claim** — e.g. regex-on-top-5-families beating the LLM classifier — reported as headline, not footnote | J§3 [W], J§12 #2 | DC | P0 |
| M-13 | Per-class classification precision/recall/F1 reported | P§6 | F | P1 |
| M-14 | Sensitivity sweep at ±30% on **three** parameters (not all) | J§6 (cut list) | F | P1 |
| M-15 | Exception list: count, reasons, ≥3 worked examples | P§6 | F | P0 |
| M-16 | Second baseline (do-nothing **and** fixed-retry-at-T+30) | P§5 vs J§6 cut list | F | P2 — **see U-07** |
| M-17 | Confusion cost: what each classifier confusion costs in rupees, ready as a panel answer | J§9 | NF | P1 |

### F. Failure handling

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| X-01 | `BUILD_LOG.md` started **on D1**, one honest entry per working session: what broke, what was assumed, what was actually wrong | J§5 [F][HARD] | NF | P0 |
| X-02 | ≥1 BUILD_LOG entry where the first hypothesis was wrong, stated plainly | J§5 [W] | NF | P0 |
| X-03 | Injected 429 on Payment Links mid-batch, reproducible by one command; backoff visible (1s/2s/4s), retry cap 3 enforced, episode → exception list, batch completes | Both (P§8, J§5) | DC | P0 |
| X-04 | Recovery number computed **excluding** the failed episode. No silent success inflation. Asserted in the eval | Both | F | P0 |
| X-05 | Backup failure scenario (duplicate-webhook replay → idempotent no-op) also works, built same day | Both (P§8, J§5 [W]) | DC | P0 |
| X-06 | Consecutive-error stopping rule visibly *not* triggered at 1 failure / threshold 3, and that is said out loud | P§8 | DC | P1 |

### G. Scope & compliance

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| S-01 | Everything in the submission works. Nothing half-wired. Checked by `make eval && make demo --dry-run && make verify-audit && make rollback` all exiting 0 | J§6 [F][HARD] | NF | P0 |
| S-02 | Out-of-scope list in README **with the regulation or reason attached to each exclusion**: mandates (NPCI retry rules), SMS/WhatsApp (TRAI DLT), card storage (RBI tokenisation), real PII (DPDP Act 2023), multi-gateway routing (that is Optimizer) | Both (P§5, J§6) | NF | P0 |
| S-03 | Cuts are visible and defended in README + BUILD_LOG, with dates | J§6 [W] | NF | P1 |
| S-04 | No claim of a messaging capability. Payment Link `notify` flags in test mode described exactly as that | Both | NF | P0 |
| S-05 | The 33%-never-re-attempted figure is attributed to Razorpay as **their own claim**. The "30% of revenue" figure is not used at all | Both (P§2, J§10) | NF | P0 |
| S-06 | Every illustrative arithmetic slide/line carries the word "illustrative", said out loud in the video | Both | DC | P0 |
| S-07 | "Not legal advice" line in README | Both | NF | P1 |
| S-08 | The pitch does **not** claim "nobody owns the payment after the session ends"; it claims the diagnosis-to-bounded-action loop | J§10 | DC | P0 |

### H. Repo & reproducibility

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| R-01 | `git clone && make setup && make eval` → `evidence/report.md` in **<5 min, with no API key**, on a clean machine | J§7 [F][HARD] | NF | P0 |
| R-02 | `evidence/report.md` is **committed** | J§7 [F][HARD] | NF | P0 |
| R-03 | README order: quickstart (3 lines) → what it does → **results table** → where the LLM is and is not → guardrails → limitations → architecture → build notes. Results visible in `head -40` | Both | NF | P0 |
| R-04 | `LIMITATIONS.md`: what is simulated, what is assumed, what breaks at scale | Both | NF | P0 |
| R-05 | `.env.example` with no secrets | Both | NF | P0 |
| R-06 | Architecture diagram treated as a named deliverable (`docs/architecture.png` + source) | J§7 | NF | P0 |
| R-07 | README written from scratch in the builder's own voice. **Every `<cite index=...>` tag and drafting artifact stripped** — the source proposal contains several | J§7 [W] | NF | P0 |
| R-08 | Repo is public | J (verified deliverables) | NF | P0 |
| R-09 | `config/guardrails.yaml` readable end-to-end in under 2 minutes (≤60 lines, commented) | J§11 | NF | P0 |

### I. Video

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| V-01 | ≤25 seconds of slides across the whole video | J§8 [F][HARD] | DC | P0 |
| V-02 | Real-time scrolling terminal output with advancing timestamps | Both | DC | P0 |
| V-03 | Razorpay test-mode dashboard on camera with a `plink_`/`pay_` ID **matching the audit record on screen** | Both | DC | P0 |
| V-04 | Sealed split opened on camera | Both | DC | P0 |
| V-05 | Idempotency proof in the video: re-run → key match → no duplicate link | Both | DC | P0 |
| V-06 | `make verify-audit` printing an intact chain, on screen | Both | DC | P0 |
| V-07 | 25 seconds on the LLM boundary: `config/policy_table.yaml` on screen + "the model never touches this file" | Both (P Step5 #3, J§8) | DC | P0 |
| V-08 | The honest sentence said out loud, then the three non-circular metrics shown | Both | DC | P0 |
| V-09 | Close names one scaling failure, unprompted | Both | DC | P1 |
| V-10 | **Delivered unscripted, in the builder's own voice.** No recital of drafted prose | J§8 [W][HARD] | DC | P0 |
| V-11 | Video length: **see U-01 — proposal says 3:00, judge says 5:00 allowed and 3:00 wastes 40% of airtime** | Conflict | DC | P0 |
| V-12 | The full arc visible in **one unbroken take**: failure → diagnose → constrained choice → human gate → execute → attribute → ledger moves. No cuts across that stretch | J§1a [W] | DC | P0 |

### J. The person (panel)

| ID | Requirement (testable) | Source | Type | Pri |
|---|---|---|---|---|
| P-01 | Can name one design decision made then reversed, and the losing option | J§9 | NF | P0 |
| P-02 | Can justify three caps-table numbers with an experiment | J§9 | NF | P0 |
| P-03 | Can state one finding that shrinks the claim | J§9 | NF | P0 |
| P-04 | Can describe a real, unplanned build failure and the wrong first hypothesis | J§9 | NF | P0 |
| P-05 | Can explain for five minutes where the model was deliberately kept out | J§9 | NF | P0 |
| P-06 | Has a prepared, honest answer to "what does a Payment Link plus a cron job not do?" | J§9 | NF | P0 |
| P-07 | Has an honest answer to "what percentage of the code did you write?" | J§9 | NF | P0 |
| P-08 | Voice consistent across README, video, and conversation | J§7/§12 | NF | P0 |

**Soft phrases, converted.** "Scalable" → E-11 (three named break points at 10k/day, in fix order). "User-friendly" → R-03 (results within `head -40`) + R-09 (policy surface readable in 2 min). "AI-powered" → the model performs exactly four jobs (C-06, C-09-selection, C-15, audit rationale) and `grep -rn 'llm_client' src/` returns call sites in no more than those modules; everything else is E-09's negative space. "Graceful degradation" → X-03/X-04. "Explainable" → every audit record carries `features_used` + `policy_rule_id` + one-line rationale. "Real-time" → explicitly **out of scope**; batch replay only, stated.

---

## 3. Judge-Gap Matrix

| Weakness raised | Why it's a real risk | Concrete engineering answer | Where it becomes visible in the product | How a judge verifies it in 90 seconds |
|---|---|---|---|---|
| **Closed evidence loop** — builder writes the distribution, the response model, and the labels, then reports a rupee figure (J§3) | Every number traces back to one author. A ±30% sweep widens a band around an invented quantity. Disclosure makes it neutral, not evidence | `make harvest` forces ~40 real failures through Razorpay test cards/VPAs, captures the actual `error_code`/`error_description`/`error_reason` strings into `evidence/harvested_errors.jsonl`, and `config/taxonomy.yaml` is rebuilt so **every class anchors on a harvested string**. Classifier is then scored on harvested strings the generator never saw | `report.md` §1 "Externally anchored inputs": a table of harvested strings → assigned class → correct/incorrect, with provenance = Razorpay test mode. Video 1:55 | `cat evidence/harvested_errors.jsonl \| head -5` — the strings are Razorpay-shaped, not builder-shaped. Cross-check one against `config/taxonomy.yaml` |
| **"Held-out test set" is not held out** — same generator, same distribution, no shift (J§3 [W]) | A panelist who knows evaluation will puncture this in one question, and the puncture contaminates every other number | Two-part fix: (a) rename to **sealed split** everywhere, (b) build `holdout/` with a genuine shift — one issuer family absent from train, plus the harvested real error strings, which the generator never produced | `report.md` header: "Sealed split, 200 episodes, with distribution shift: issuer family `BANK_E` unseen in train; 12 real harvested error strings unseen in train." Video 1:50 | `diff <(jq -r .issuer data/train.jsonl \| sort -u) <(jq -r .issuer holdout/sealed.jsonl \| sort -u)` shows the shift |
| **Sensitivity sweep perturbs the builder's own parameters** (J§3) | It looks like rigour and isn't. A range around a fiction is still a fiction | Reorder, reframe, and demote. Non-circular metrics lead; the recovery range is presented in third position and labelled a **design target under stated assumptions**, not a measurement. Sweep narrowed to three parameters | `report.md` section order is literally: 1. What was measured (non-circular) → 2. What was externally anchored → 3. Design target under assumptions. Metrics screen at 1:40 | `head -30 evidence/report.md` — the first number on the page is guardrail correctness, not rupees |
| **Failure handling answers the wrong question** — injected 429 is *designed* failure, not "what broke during development" (J§5) | Razorpay's process asks what broke and how you recovered. A staged demo does not answer it, and the gap shows in the panel | `BUILD_LOG.md` from D1, one entry per session, with ≥1 entry naming a wrong first hypothesis. Cannot be reconstructed later | Committed at repo root, linked from README "Build notes". Referenced verbally in the close | `cat BUILD_LOG.md` + `git log --format='%ad %s' --date=short -- BUILD_LOG.md` — 11 dated commits, not one |
| **Scope is an 80–120h build in a 33h budget** (J§6) | Three half-wired subsystems score below one working one. Attempted scope is not rewarded | Cut now, not on D9: web approval UI → JSON queue + `make approve`; Hinglish → English only; sensitivity breadth → 3 params. ~12h recovered, ~8h of it re-spent on harvest + threshold experiments + rehearsal | README "What I cut and why", with dates. The absence of a web UI is stated as a decision, not hidden | `make eval && make demo && make verify-audit && make rollback` all exit 0. Nothing errors, nothing is stubbed |
| **Guardrails described rather than demonstrated** (J§2 borderline tier) | "Borderline" tier is defined by exactly this | Every guardrail check emits a live line during the run and a record in the audit log. Fault-injection harness produces the 0-breach counts over 200 real link creations | Run stream shows a per-episode guardrail ticker: `quiet_hours ✓ freq_cap ✓ amount_cap ✓ dedup ✓`. Video 1:05–1:20 | Watch one episode go past. Then `jq '.guardrail_checks' evidence/audit_sample.jsonl \| head` |
| **Arbitrary thresholds** (J§4 [W], J§12 #3) | "I will pick one at random and ask." A shrug here undoes the whole guardrail story | `experiments/thresholds/` — three scripts producing three plots: auto-approve ceiling (₹2k vs ₹5k vs ₹10k → gate-fire rate and exposure), outage-cluster size (10 vs 15 vs 25 → false-escalation rate), retry cap (3 vs 10 → wasted calls and time-to-exception) | Each line in `config/guardrails.yaml` carries a comment: `# 5000 — see experiments/thresholds/auto_approve.md; at 2000 the gate fires on 61% of episodes and stops being a signal` | `cat config/guardrails.yaml` — every number has a reason on the same line |
| **Binary escalation** (J§1c [W]) | The bar says "compliant escalation"; auto/human is the minimum reading, not the good one | Three bands in config: `auto` (≤₹5,000, low-risk cause), `human_keystroke` (>₹5,000 or batch >50 contacts), `hard_refuse` (issuer-outage cluster >15, opted-out, already-paid, age >72h). Each writes a distinct `escalation_tier` + reason | Run stream colour-codes the tier per episode; the outage cluster visibly refuses 40 episodes at once | `jq -r '.escalation_tier' evidence/audit_sample.jsonl \| sort \| uniq -c` — three tiers present |
| **No result that shrinks the builder's claim** (J§3 [W], J§12 #2) | The rarest signal in the process, and currently absent | Build the regex baseline classifier deliberately and score it head-to-head per error family. If regex wins on the top five families, that is the headline sentence | `report.md` §2: "Where the model loses: regex beats the LLM on `BAD_REQUEST_ERROR` families by X points. The model earns its place only on the unmatched tail (Y% of episodes)." Video 2:00 | It is a heading in the report, not a footnote. `grep -i "where the model loses" evidence/report.md` |
| **Overstating the gap** — "nobody owns the payment after the session ends" (J§10) | Invites "is this a feature we already ship?" — the most dangerous question in the room | Rewrite §2 of the pitch to name the seam precisely, and prepare the cron-job answer (P-06) | The pitch sentence, the README "What it does", and the video 0:12–0:35 diagram all say the same narrower thing | Listen to the first 30 seconds. If the phrase appears, the flag fires |
| **Vendor numbers laundered into facts** (J§10) | One caught laundered number discounts every other number | Use only the 33%-never-re-attempted figure, attributed on screen as *Razorpay's own claim*. Drop the "30% of revenue" figure entirely. Every merchant-arithmetic line carries "illustrative" | Video 0:00–0:12 lower-third reads "Razorpay's own figure". Slide text says "illustrative" | Watch 12 seconds. Read the README problem section |
| **Repo requires debugging** (J§7 [F][HARD]) | "I close the tab and you never find out why" | `make setup` installs from a pinned `requirements.txt` into a venv; `make eval` runs entirely on committed fixtures + a committed LLM response cache. No key path in the eval | `evidence/report.md` is committed, so results exist before anything runs | `git clone && make setup && make eval` on a clean machine with no `.env` |
| **Idempotency / webhook dedup / out-of-order** (J§4 [F][HARD]) | The 30-second read that separates payments engineers from app developers | Key = `sha256(payment_id + ':' + policy_rule_id)[:32]`, used as Payment Link `reference_id` **and** as a SQLite `UNIQUE` constraint. Dedup on `payment_id` at ingest. Terminal-state table prevents out-of-order regressions | The re-run demo: same key, `duplicate_suppressed`, zero new links, and the Razorpay dashboard link count unchanged | Video 2:20–2:30, then `sqlite3 second_rail.db '.schema executions'` |
| **Reversibility buried** (J§4 [F][HARD]) | A sentence in a paragraph is not a claim a panelist can find | `## No code path in Second Rail moves money` as a top-level README heading, immediately after the results table | README, and `make rollback` cancelling every link a run created | `head -40 README.md` contains the heading. Then run `make rollback RUN_ID=run_007` |
| **Pre-registration unverified** (J§3 [F][HARD]) | "That last line is not hypothetical. Pre-registration is checkable and I check it." | Commit `outcome_model.md` on D1, before `src/attribute/` exists. Never amend it; corrections go in an appendix with their own commit | `outcome_model.md` at repo root, linked from the metrics section of the report | `git log --format='%ad %s' --date=short \| tail -30` — outcome_model precedes the first eval commit |
| **False-positive cost skipped** (J§3) | "Most submissions skip this entirely; it is a cheap differentiator" | Count and price every contact to someone already paid, opted out, or inside the frequency window: ₹0.20/SMS + a **stated, sourced-as-assumption** goodwill proxy. Report `net = gross − fp_cost` | The metrics screen shows gross, FP cost, and **net** — net in the largest type | `grep -A3 "Net recovery" evidence/report.md` |
| **Authorship seam** (J§9 [W][HARD], J§12 #4) | "A panel built specifically to bypass resumes will find the seam in about four minutes" | README written from scratch; every drafting artifact stripped; BUILD_LOG in first person; video unscripted; the five P-0x answers rehearsed as *recall*, not recital | Voice consistency across README, `where-the-llm-is-not.md`, BUILD_LOG, and video | Read three paragraphs from three files. Do they sound like one person? |
| **Audit described, not verifiable** (J§1e [W]) | "Verifiable beats described, always" | `make verify-audit` walks the chain in <2s and prints `chain intact — N records`; a `--tamper-test` flag proves it can fail | Final 15 seconds of the video | Run it. Then edit one byte of the JSONL and run it again |

---

## 4. Architecture

### Stack — one line of justification each

| Layer | Choice | Why this and not the impressive alternative |
|---|---|---|
| Language | **Python 3.11** | The Razorpay SDK is first-class here, and the eval harness is data work; 3.11 over 3.12 only because wheel availability is more certain on a clean judge machine |
| Webhook receiver | **FastAPI 0.115.x + uvicorn[standard] 0.32.x** | Signature verification and a 200-in-under-50ms ack are the entire job; Flask would also work, FastAPI gives request validation free |
| Schemas | **Pydantic 2.9.x** | Episode, audit record, and LLM structured output all need strict validation at boundaries; a malformed LLM response must fail loudly, not propagate |
| Persistence | **SQLite (stdlib `sqlite3`, WAL mode)** | Single file, zero setup on the judge's machine, and `UNIQUE` constraints do real idempotency work. Postgres would add a Docker dependency to R-01 and buy nothing at 600 episodes |
| Audit store | **Append-only JSONL on disk** (`evidence/audit/run_*.jsonl`) | Hash chain verification must be readable by a human with `jq`; a database row is less convincing on camera than a file you can `tail -f` |
| CLI | **Typer 0.12.x** | `make` targets wrap it; subcommand structure maps 1:1 to the loop stages |
| Terminal UI | **Rich 13.9.x** (`Live`, `Table`, `Progress`) | This is the demo surface. Scrolling, colour-coded guardrail ticker, advancing timestamps — exactly what J§8 asks to see, at ~4h less cost than a web UI |
| HTTP | **razorpay Python SDK 1.4.x** for orders/payments/customers; **httpx 0.27.x** direct for Payment Links | The SDK is thin over the REST API; using httpx for the executor means the raw request/response and status code land in the audit record verbatim |
| Retry/backoff | **Hand-rolled, ~40 lines**, `src/execute/retry.py` | `tenacity` hides attempt count behind a decorator; the audit record needs `attempt: 2, delay_ms: 2000` written explicitly, and the retry cap must be a config value a judge can read |
| Config | **PyYAML 6.0.2** | `taxonomy.yaml`, `policy_table.yaml`, `guardrails.yaml` are the artifacts a panelist reads. YAML over JSON purely because comments carry the threshold justifications (E-08) |
| LLM | **Primary: Gemini 2.5 Flash** (cheap, generous free tier, no card required in IN). **Fallback: `gpt-4o-mini`** behind one interface | Confirm current model ID and per-token price on D1. The disk cache makes provider swap a config change, so this is deliberately a reversible decision |
| LLM cache | **Content-addressed disk cache**, `cache/{sha256(model+prompt)}.json`, committed | This is what makes R-01 work with no API key. It is also the LLM-cost metric's source of truth |
| Charts | **matplotlib 3.9.x → committed PNGs** | Static images in `evidence/charts/`. No JS, no build step, renders on GitHub and on a phone |
| Tunnel | **cloudflared** quick tunnel | Free, no account, one binary; ngrok's free tier now churns URLs and requires signup, which is one more thing to break on stage |
| Tests | **pytest 8.3.x** | Idempotency, dedup, out-of-order, cap-breach, admissibility are all unit-testable and each one is a claim in the ledger |
| Diagram | **Excalidraw → `docs/architecture.png` + committed `.excalidraw` source** | It is a named deliverable (R-06), so the source must be editable, not a screenshot |

**Deployment target: none.** Second Rail runs locally. `make eval` runs on a judge's laptop with no key and no network. `make demo` runs on the builder's laptop with `.env` populated and a cloudflared tunnel exposing `POST /webhooks/razorpay`. There is no cloud deploy, no Docker, no CI/CD — and that absence is stated in `LIMITATIONS.md` as a deliberate choice, because a hosted service adds a live dependency to the one artifact (R-01) that must never fail. *(See U-09 if a hosted endpoint is wanted for judge-initiated demos.)*

### Service boundaries

Six modules, each a directory under `src/`, each with one job and a typed interface:

```
src/ingest/     webhook receipt, signature verify, dedup, normalization      [no LLM]
src/gate/       eligibility checks, caps, quiet hours, frequency             [no LLM]
src/diagnose/   regex baseline + LLM classifier + confidence + rationale     [LLM]
src/choose/     policy engine → admissible set; LLM selects 1 of ≤3          [LLM, constrained]
src/execute/    idempotency, retry/backoff, Payment Links, rollback          [no LLM]
src/attribute/  outcome listener, attribution window, ledger                 [no LLM]
src/audit/      hash chain, append, verify                                   [no LLM]
```

The boundary that matters: **`src/gate/`, `src/execute/`, `src/attribute/` and `src/audit/` import no LLM client.** That is enforced by a test (`test_llm_boundary.py`) that greps those packages for the client symbol and fails if found. `docs/where-the-llm-is-not.md` points at that test.

### Data flow — one episode, end to end

```
[1] Razorpay test mode: forced failure (test card / test VPA)
        │  payment.failed
        ▼
[2] cloudflared tunnel → POST /webhooks/razorpay (FastAPI)
        │  verify X-Razorpay-Signature (HMAC-SHA256, webhook secret)
        │  200 OK returned in <50ms — processing is queued, not inline
        ▼
[3] src/ingest — dedup on payment_id (SQLite UNIQUE) → normalize to Episode
        │  duplicate? → audit(suppressed, dedup_replay) → STOP
        ▼
[4] src/gate — 7 deterministic checks, fixed order, each audited
        │  ineligible? → exception list + audit(suppressed, <reason>) → STOP
        ▼
[5] src/diagnose — regex baseline first; unmatched tail → LLM call #1
        │  cache hit? no network. cache miss? provider call, response cached to disk
        │  → cause, confidence, rationale
        ▼
[6] src/choose (deterministic) — policy_table.yaml → admissible set ≤3
        │  → LLM call #2: pick one + name features used + draft copy
        │  outside the set? → halt run, audit(admissibility_violation)
        ▼
[7] src/gate (re-check) — caps, DND, quiet hours, idempotency key
        │  → escalation tier: auto | human_keystroke | hard_refuse
        ▼
[8] approval queue (JSON file) — human tier blocks until `make approve`
        ▼
[9] src/execute — POST https://api.razorpay.com/v1/payment_links
        │  reference_id = sha256(payment_id:policy_rule_id)[:32]  ← server-side dedup
        │  expire_by set; notify.sms/email flags; callback_url → tunnel
        │  429/5xx → backoff 1s/2s/4s, cap 3 → execution_failed → exception list
        ▼
[10] persistence — SQLite: executions, audit JSONL appended + hash-chained
        ▼
[11] outcome: payment_link.paid / payment.captured / payment_link.expired
        │  → src/attribute — inside attribution window? → ledger entry
        ▼
[12] response surface — Rich live stream (during run) + evidence/report.md (after)
```

**Where the agent layer sits:** strictly between step [5] and step [7], and nowhere else. It receives an already-gated episode and an already-constrained action set, and it returns a classification and a selection. It never sees a cap value, never computes an amount, never learns whether an episode was recovered. Two calls per episode, both cached by content hash.

---

## 5. API + Integration Surface

| Service | Purpose | Auth | Rate limits | Failure mode | Fallback when down |
|---|---|---|---|---|---|
| `POST /v1/orders` | Create synthetic orders so payments can be forced to fail during harvest | HTTP Basic, `rzp_test_key:secret` | **Not publicly pinned by Razorpay — treat as unknown.** Self-throttle at 2 req/s with a token bucket and say so in LIMITATIONS | 4xx on bad payload; 5xx transient | Harvest is a one-time D2 job; on failure, retry next session. Not on the demo path |
| `GET /v1/payments/{id}` | Read the `error_code` / `error_description` / `error_source` / `error_step` / `error_reason` object — **verify exact field names on D1, this is load-bearing** | HTTP Basic | as above | Missing/renamed field → normalizer raises, episode → exception list with `reason: schema_drift` | Recorded fixtures in `fixtures/payments/` |
| `POST /v1/payment_links` | **The only external effect.** Create a cancellable link with `expire_by`, `reference_id`, `notify.sms`/`notify.email`, `callback_url` | HTTP Basic | as above | 429 → backoff 1/2/4s, cap 3 → `execution_failed`. Duplicate `reference_id` → Razorpay rejects → recorded as `duplicate_suppressed` (this is a *success* path, not an error) | `FixtureExecutor` behind the same `Executor` interface, scaffolded D3. README states per-run which calls were live vs replayed |
| `POST /v1/payment_links/{id}/cancel` | Reversibility. Powers `make rollback RUN_ID=X` | HTTP Basic | as above | Already-paid links cannot cancel → report as `cancel_declined` with reason, do not swallow | Rollback prints the link IDs it could not cancel and exits non-zero |
| `POST /v1/customers` | Create the synthetic customer set once | HTTP Basic | as above | Duplicate contact → fetch instead of create | Local customer table only |
| Razorpay Webhooks | `payment.failed`, `payment.captured`, `payment_link.paid`, `payment_link.expired` | HMAC-SHA256 over raw body, `X-Razorpay-Signature`, webhook secret from dashboard | Razorpay retries on non-2xx — which is exactly why C-02 dedup exists | Tunnel down → events lost | **Poll fallback:** `GET /v1/payment_links/{id}` every 20s for the demo's single link. Built D8, 20 minutes, and it removes the tunnel from the critical demo path |
| cloudflared quick tunnel | Public HTTPS → localhost:8000 | none | none | URL changes on restart → dashboard webhook config goes stale | Poll fallback above; plus a pre-recorded webhook replay file, `make replay-webhooks` |
| LLM provider (Gemini 2.5 Flash primary) | Classification tail + constrained action selection + copy + rationale | API key in `.env` | Free tier is RPM-limited; throttle to 1 req/s and batch nothing | Timeout/429/quota → **regex baseline result is used**, `llm_degraded: true` written to the audit record, episode still completes | Committed disk cache serves every eval run. `make eval` never touches the network |
| Razorpay published error-code docs | External label source for M-09 | none (public) | none | Docs drift | Snapshot the table into `evidence/razorpay_error_codes_snapshot.md` on D2 with the fetch date |
| GitHub | Public repo delivery | — | — | — | — |

**Nothing else.** No queue (SQLite + in-process worker is sufficient at 600 episodes, and E-11 names Redis as the first thing that breaks at 10k/day). No auth provider. No email/SMS provider — that would be a TRAI DLT claim Second Rail is not making.

---

## 6. Data Model

SQLite. `PRAGMA journal_mode=WAL`. All timestamps ISO-8601 with `+05:30` offset.

**customer** — `customer_id` PK (`cust_*`), `synthetic_name`, `contact_hash` (sha256, never a raw phone), `email_hash`, `segment` ENUM(first_time, repeat, high_value), `opted_out` BOOL, `opt_out_ts`, `created_at`
  · IDX `(opted_out)`

**episode** — `episode_id` PK (ULID), `payment_id` UNIQUE (`pay_*`), `order_id`, `customer_id` FK, `amount_paise` INT, `currency`, `instrument` ENUM(upi, card, netbanking, wallet), `issuer_family`, `error_code`, `error_description`, `error_source`, `error_step`, `error_reason`, `failed_at`, `received_at`, `split` ENUM(train, sealed), `is_synthetic` BOOL, `harvested_from` NULLABLE (points to a real harvested string)
  · UNIQUE `(payment_id)` ← the dedup boundary · IDX `(customer_id, failed_at)` ← frequency cap · IDX `(split)` · IDX `(issuer_family, error_code)`

**harvested_error** — `harvest_id` PK, `payment_id`, `error_code`, `error_description`, `error_source`, `error_step`, `error_reason`, `instrument`, `captured_at`, `forced_by` (which test card/VPA), `assigned_class` FK, `doc_reference` (Razorpay error-doc anchor for M-09)
  · This table is the **non-circular anchor**. Committed as JSONL too.

**taxonomy_class** — `class_id` PK, `label`, `definition`, `anchor_error_strings` JSON, `recoverable_in_principle` BOOL, `source` ENUM(harvested, doc, inferred)

**gate_check** — `check_id` PK, `episode_id` FK, `check_name`, `result` ENUM(pass, fail), `reason`, `evaluated_at`, `order_index`
  · IDX `(episode_id, order_index)`

**diagnosis** — `diagnosis_id` PK, `episode_id` FK UNIQUE, `method` ENUM(regex, llm, regex_then_llm), `class_id` FK, `confidence` REAL, `rationale` TEXT, `llm_model`, `prompt_hash`, `cache_hit` BOOL, `latency_ms`, `cost_paise`, `llm_degraded` BOOL

**policy_rule** — `policy_rule_id` PK (`P-14`), `cause_class` FK, `amount_band`, `segment`, `instrument`, `admissible_actions` JSON (≤3), `escalation_tier` ENUM(auto, human_keystroke, hard_refuse), `justification` TEXT
  · UNIQUE `(cause_class, amount_band, segment, instrument)` ← guarantees determinism

**decision** — `decision_id` PK, `episode_id` FK UNIQUE, `policy_rule_id` FK, `candidate_actions` JSON, `chosen_action`, `features_used` JSON, `inside_admissible_set` BOOL, `escalation_tier`, `decided_at`
  · IDX `(inside_admissible_set)` ← powers the admissibility-rate metric

**approval** — `approval_id` PK, `episode_id` FK, `required` BOOL, `tier`, `approved_by`, `approved_at`, `rejected_reason`, `expires_at`
  · IDX `(required, approved_at)` ← the pending queue

**execution** — `execution_id` PK, `episode_id` FK, `idempotency_key` (sha256[:32]), `reference_id`, `api` ('paymentLink.create'), `plink_id`, `short_url`, `request_body_hash`, `response_code`, `attempt` INT, `delay_ms`, `status` ENUM(created, duplicate_suppressed, failed, cancelled), `created_at`, `cancelled_at`
  · **UNIQUE `(idempotency_key)`** ← the single most load-bearing constraint in the schema · IDX `(run_id, status)`

**webhook_event** — `event_id` PK (Razorpay's), `event_type`, `payment_id`, `plink_id`, `raw_body_hash`, `signature_valid` BOOL, `received_at`, `processed` BOOL, `dedup_result` ENUM(new, duplicate, out_of_order)
  · UNIQUE `(event_id)` · IDX `(payment_id, received_at)` ← out-of-order detection

**attribution** — `attribution_id` PK, `episode_id` FK, `execution_id` FK, `outcome` ENUM(recovered, not_recovered, pending, suppressed, execution_failed), `recovered_amount_paise`, `window_hours`, `attributed_at`, `attribution_rule_id`

**ledger_entry** — `entry_id` PK, `run_id` FK, `episode_id` FK, `kind` ENUM(gross_recovery, fp_cost, net), `amount_paise` INT (signed), `basis` TEXT
  · IDX `(run_id, kind)`

**run** — `run_id` PK (`run_007`), `started_at`, `ended_at`, `mode` ENUM(dry_run, execute, fixture), `episode_count`, `git_sha`, `config_hash`, `stopped_reason` NULLABLE, `llm_cost_paise`, `throughput_epm`

**exception_entry** — `exception_id` PK, `run_id` FK, `episode_id` FK, `stage`, `reason_code`, `reason_text`, `excluded_from_recovery` BOOL

**audit_record** — JSONL on disk, mirrored to a table for indexing: `event_id` PK (ULID), `run_id`, `episode_id`, `actor` ENUM(agent, human, system), `stage` ENUM(gate, diagnose, choose, approve, execute, attribute, rollback, stop), `inputs_hash`, `prev_hash`, `hash`, `seq` INT
  · UNIQUE `(run_id, seq)` · the chain is verified from the file, not the table

**Relationships:** customer 1─N episode · episode 1─N gate_check · episode 1─1 diagnosis · episode 1─1 decision · episode 1─0..1 approval · episode 1─0..N execution (N>1 only when a retry produced a new attempt row; the UNIQUE key guarantees ≤1 `created`) · episode 1─0..1 attribution · run 1─N everything.

---

## 7. UI Surface Map

The web approval UI is **cut** (J§6 cut list, P§5 first cut). The surface is a Rich terminal application plus committed artifacts. Every state below must exist before D9 freeze.

| # | Surface | States |
|---|---|---|
| **S1** ⭐ | **`make demo` — live run stream** | **Empty:** no episodes match the filter → "0 eligible episodes; 12 suppressed. See exception list." **Loading:** config hash + run_id + mode banner, then "sealed split locked, checksum ✓". **Streaming:** one line per episode with advancing timestamp, cause + confidence, admissible set, guardrail ticker (`quiet_hours ✓ freq_cap ✓ amount_cap ✓ dedup ✓`), tier badge. **Partial:** `llm_degraded → regex fallback used` inline in amber. **Blocking:** approval prompt halts the stream on a >₹5,000 episode, waiting on a keystroke. **Error:** 429 → visible `attempt 1 · backoff 1s`, `attempt 2 · 2s`, `attempt 3 · 4s`, then `execution_failed → exception list`. **Refusal:** outage cluster → 40 episodes collapse into one red `hard_refuse · shared_cause_cluster=40 · escalated`. **Success:** summary panel — counts by outcome, throughput, LLM cost in ₹, `run_007 complete`. |
| **S2** | `make approve` — approval queue | **Empty:** "queue empty". **Pending:** table of episode, amount, cause, chosen action, reason for gating. **Approved / Rejected:** confirmation + audit record id. **Expired:** items past `expires_at` shown struck through, auto-refused with reason. |
| **S3** ⭐ | `evidence/report.md` (committed, rendered) | **Section 1 — What I measured (non-circular):** guardrail correctness over 200 real link creations, admissibility rate, throughput + ₹ LLM cost. **Section 2 — Externally anchored:** harvested error strings, classifier vs regex head-to-head including where the model loses. **Section 3 — Design target under stated assumptions:** recovery **range** vs baseline, FP cost, **net**. **Section 4:** exception list + 3 worked examples. **Section 5:** what this does not measure. Plus: **stale state** — if `report.md` git SHA ≠ current SHA, `make eval` prints a warning. |
| **S4** | `make verify-audit` | **Success:** `chain intact — 1,842 records (0.4s)`. **Failure:** `chain BROKEN at seq 417 — expected sha256:ab… got sha256:cd…`. **Empty:** "no audit file for run_id". |
| **S5** | `make rollback RUN_ID=X` | **Empty:** "run created 0 links". **Success:** per-link table, `cancelled 7/7`. **Partial:** `cancelled 6/7 — plink_X already paid, cannot cancel` + non-zero exit. **Error:** API unreachable → prints the link IDs so a human can cancel manually. |
| **S6** | `make harvest` (D2, one-time) | **Success:** "captured 41 real error objects across 6 instruments → evidence/harvested_errors.jsonl". **Partial:** "3 test instruments did not produce a failure; recorded which". **Error:** keys missing → explicit instruction, exit 2. |
| **S7** ⭐ | **Razorpay test-mode dashboard** (external) | Payment Links list showing the `plink_` created live, its `reference_id`, and — critically — **the count not incrementing** on the idempotency re-run. This is not our software but it is a demo screen and it is the only third-party corroboration in the submission. |
| **S8** | `make eval` (no-key path) | **Loading:** "fixture mode — 0 network calls". **Success:** report path + elapsed. **Error:** any missing fixture names the file, never a stack trace. |
| **S9** | README first screen | Quickstart (3 lines) → what it does → results table → **"No code path in Second Rail moves money"** heading. Must all fit inside `head -40`. |

**The three screens that carry the demo: S1, S3, S7.** S1 proves the loop closes and the guardrails fire. S3 proves the evidence is ordered honestly. S7 is the only pixel in the submission that the builder did not render, and it is the one that makes the rest believable. S4 is the closer.

---

## 8. Demo Script — 3:00, second by second

⚠️ **Written to the 3:00 requested. The judge file states five minutes are allowed and that 3:00 leaves 40% of airtime unspent — see U-01.** A 5:00 expansion plan follows the table; the 3:00 cut below is the spine and every added beat extends it rather than rewriting it.

| Time | Beat | On screen | Said (unscripted — these are the *points*, not a script to read) | Judge clause satisfied |
|---|---|---|---|---|
| 0:00–0:10 | Hook | Single figure on black + lower-third "Razorpay's own figure" | Roughly a third of failed transactions are never re-attempted — that is Razorpay's number, not mine. This is what came back. | J§10 (attribution), J§8 (≤25s slides — this is 10) |
| 0:10–0:30 | The seam | One diagram: in-session (Optimizer, IPR) → session ends → post-session diagnosis loop | Razorpay fights the in-session failure. Payment links and abandoned-checkout recovery exist too. What is missing is *automated per-episode diagnosis driving one bounded action*. That narrow thing is what I built. | J§10 (no overstated gap), J§1a |
| 0:30–0:40 | The claim, ordered | `head -30 evidence/report.md` | Before the money number, here is what I actually measured: zero duplicate links, zero cap breaches, zero quiet-hour contacts across 200 real test-mode link creations. | J§3 (non-circular first), J§12 #1 |
| 0:40–1:35 | **The unbroken run** — no cuts | Terminal (S1) left, Razorpay dashboard (S7) right | `make demo --execute`. Episodes stream, timestamps advance. Drill into one: real `error_code` harvested from Razorpay's own test-mode failure, classification + rationale, three admissible actions, guardrail ticker passing one by one. Then the gate fires on a ₹12,400 payment — I approve with one keystroke — a `plink_` appears in the dashboard on the right, and the ID matches the audit line on the left. Payment completes. Ledger moves. | **J§1a [W] (full arc, one take)**, J§1c, J§1e |
| 1:35–1:45 | The refusal | Outage cluster collapses to one red line | Forty episodes share a cause. The correct answer is not forty messages. It suppresses, escalates, and writes the reason. That is the third escalation tier — auto, human, refuse. | J§1c [W] (tiered escalation) |
| 1:45–2:00 | The boundary | `config/policy_table.yaml` + `config/guardrails.yaml` full-screen | Every money-adjacent decision is in these two files. The model never touches them. It classifies language and picks one option out of a set it did not construct. `docs/where-the-llm-is-not.md` is the list of what I refused to give it. | J§8 [F] (25s to the boundary), J§4 [W] |
| 2:00–2:20 | Evidence, honestly | Sealed split opened on camera; report S3 | Checksum verified, split opened now. Classification against strings I did not write. **And here is where I lose:** regex beats my classifier on the top error families — the model only earns its place on the unmatched tail. Recovery is a range, from an assumption file committed before I ran anything. This is a simulator; here are the three numbers that do not depend on it. | J§3 [F][HARD] ×2, **J§12 #2 (result that shrinks the claim)**, J§1b |
| 2:20–2:42 | Failure + idempotency | 429 injected; then the re-run | Backoff 1, 2, 4 — retry cap 3, so it stops rather than hammering. Episode goes to the exception list and the recovery number excludes it. Now the five seconds that matter: I re-run that episode. Same idempotency key. No duplicate link. Dashboard count unchanged. | J§5 [F], J§4 [F][HARD], J§8 [F] |
| 2:42–2:55 | Verify | `make verify-audit` | `chain intact — 1,842 records`, under two seconds. Every decision, hash-chained. | J§1e [W] |
| 2:55–3:00 | Close | Repo URL | At 10k episodes a day this breaks in three places, and the first is the in-process dedup store. Repo is here. | J§8 [W], J§4 [W] |

**The 5:00 expansion (if U-01 resolves to five minutes), inserted, not rewritten:**
`+0:35` after 0:30 — `make harvest` on screen: forcing real failures with Razorpay test instruments and the actual strings landing in `evidence/harvested_errors.jsonl`. This is the single highest-value beat available and it currently has no airtime.
`+0:30` after 1:45 — the threshold experiments: why ₹5,000 and not ₹2,000, on a plot.
`+0:25` after 2:20 — `make rollback` cancelling every link the run created, live.
`+0:20` at 2:55 — one entry from `BUILD_LOG.md` read aloud: the wrong first hypothesis.
`+0:10` — breathing room in the run so the terminal is legible.

**Recording rules:** the 0:40–1:35 stretch is one take, no cuts (J§1a [W]). Total slide time 0:00–0:30 = 30s — **trim to ≤25s** or the hard gate fires. Record on D10 with at least three usable takes.

---

## 9. Risk Register — five things most likely to break live

| # | Risk | Why it breaks on stage specifically | Mitigation (built before D9) |
|---|---|---|---|
| 1 | **Tunnel drops or the webhook never arrives during the 0:40–1:35 unbroken take** | cloudflared quick-tunnel URLs die on reconnect, and the dashboard webhook config goes stale silently. This is the only beat that cannot be cut and cannot be faked | Build the **poll fallback** on D8 (20 min): after creating the link, `GET /v1/payment_links/{id}` every 20s and treat a `paid` status as the outcome event. The tunnel becomes an optimisation, not a dependency. Rehearse the demo once with the tunnel deliberately killed |
| 2 | **Razorpay's test-mode failure-simulation instruments do not behave as documented** | The proposal itself flags low confidence on the exact test card/VPA strings, and the entire harvest (M-08) plus the live failure both depend on them. Discovering this on D5 costs the highest-value differentiator | **D1, hour 2**: verify one forced failure end to end and paste the exact strings into `BUILD_LOG.md`. If instruments do not cooperate, fall back to M-09 (evaluate against Razorpay's published error-code docs) as the external anchor — still non-circular, 1h instead of 3h |
| 3 | **LLM provider quota or latency stalls the live run** | Free tiers rate-limit unpredictably, and a 15-second pause mid-stream reads as a broken product on camera | The demo run uses the **committed disk cache by default**; `--execute` affects Razorpay calls only, not LLM calls. A cache miss falls through to the regex baseline with a visible amber `llm_degraded` line, which is itself a good look. Pre-warm the cache for the exact demo episode set on D10 |
| 4 | **The 429 injection rig fires at the wrong episode or not at all** | It is a stateful fault injector interacting with retry logic and a live API; the most common failure is a rig that works once and not on take three | The rig is deterministic on episode index (`--inject-429-at 7`), reset by `make demo` each run. **Backup scenario built the same day (X-05):** duplicate-webhook replay showing an idempotent no-op — 20 minutes, nearly as convincing, and it needs no external API to misbehave |
| 5 | **The approval keystroke beat hangs, or fires on the wrong episode** | The gate must fire on a specific >₹5,000 episode inside a one-take stretch. If episode ordering shifts (seed drift, cache state), the beat lands in the wrong place or never | Pin the demo episode set with an explicit seed and a committed `demo/episode_order.json`; assert on run start that episode #N is the ₹12,400 one and abort loudly before recording if not. Approval prompt has a 60s timeout that auto-refuses with a reason rather than hanging |

**Sixth, non-stage but higher expected cost:** the 33h ceiling. The judge file asks to cut ~12h and then adds ~8h of new work (harvest 3h, threshold experiments 1h, README from scratch 2h, unscripted rehearsal 2h). Net −4h, which is thin. Tracked as U-12.

---

## 10. Unknowns — questions for you

Nothing below has been silently decided. Each one changes scope, a claim, or the schedule.

**U-01 · Video length — direct conflict.** Proposal §10 specifies a 3:00 video. The judge file §8 says five minutes are allowed and that 3:00 wastes 40% of the most attention-limited artifact in the process. You asked me for a 3-minute script and I wrote one. Do we ship 3:00, or do I extend to the 5:00 plan sketched in §8? *(Affects: recording day, whether harvest and rollback get airtime at all.)*

**U-02 · Hard gates G-01 and G-02.** Are you a currently enrolled student, and can you be in Bangalore from September for 6–12 months? The judge file says if either fails, stop. I need a yes before this blueprint is worth executing.

**U-03 · Test keys (G-03).** Have you applied? Both files make this D1 hour 1, and Risk #2 escalates sharply if keys land after D3.

**U-04 · Taxonomy ordering — schedule conflict.** Proposal §9 freezes the 9-class taxonomy on D1. Judge §3 [W][HARD] requires the taxonomy be built *from harvested real error strings*, which needs keys. These cannot both happen on D1. My proposed resolution: freeze a **provisional** taxonomy D1, harvest D2, ratify or amend D2 evening, freeze hard after that — with the amendment visible in git history as evidence of the harvest driving the design. Do you accept that, and does the class count stay at nine if the real strings suggest seven or eleven?

**U-05 · Sealed split — shift or rename?** Judge §3 [W] says the 200 episodes are not a held-out test set because train and test come from the same generator. Two options with different costs: **(a)** introduce a genuine shift (hold out an issuer family + inject harvested strings the generator never produced) — roughly 2h, and it makes the generalisation claim real; **(b)** rename to "sealed split" everywhere and claim only what it is — 20 minutes. I lean (a) *and* the rename. Your call, because (a) will likely lower your recovery number.

**U-06 · Does the recovery number remain the pitch?** The proposal's one-line pitch is built on "the rupees it actually brought back." Judge §3 says that number must be third in order and framed as a design target. Those are compatible in the report but they fight in the *pitch sentence*. Do I rewrite the pitch around the bounded-loop-plus-honest-accounting claim, or keep rupees as the hook with the framing attached?

**U-07 · Baselines.** Proposal §5 requires two (do-nothing, fixed-retry-at-T+30). Judge §6 lists the second baseline in the cut list. Judge §1b requires *at least one*. Which survives? Do-nothing is cheaper and weaker; fixed-retry is the one that actually tests whether diagnosis beats a schedule — and it is also the honest answer to the panel's "what does a Payment Link plus a cron job not do?" I lean fixed-retry only.

**U-08 · Attribution window length is undefined in both files.** Proposal §3 says "within the attribution window" and never gives a number. Judge §1b says a number with an undefined attribution window fails. What is it — 24h? 48h? 72h? This needs to be in `outcome_model.md` on D1 and it materially moves the recovery figure.

**U-09 · Deployment.** I have assumed local-only: `make eval` offline, `make demo` on your laptop behind a tunnel, no cloud. Neither file specifies. Do you want a hosted webhook endpoint (adds a live dependency to the demo, but lets a judge trigger a run themselves)?

**U-10 · The goodwill proxy for false-positive cost has no value in either file.** ₹0.20/SMS is stated; the goodwill figure is not. Pick a number and a justification now (it goes in the pre-registered `outcome_model.md`) or it becomes an invented figure a panelist can puncture.

**U-11 · LLM call budget — internal contradiction in the proposal.** The assumptions table caps LLM calls at "~1 per episode" for cost. §4 requires two sequential calls (classify → policy engine constrains → select), because the selection call cannot happen before the deterministic policy engine has run. Two calls × 600 episodes = 1,200 calls. Confirm the budget accommodates that, or accept a merged single call at the cost of the constrained-selection design (I do not recommend this — the boundary is the strongest thing in the submission).

**U-12 · The hour budget.** Judge cuts ≈12h and adds ≈8h of new mandatory work. Net gain is ~4h against a scope the judge estimates at 80–120h. Which of these do you want to cut *further*: the demo terminal UI polish, per-class classification metrics (M-13), the regex head-to-head (M-12 — I would not cut this, it is a winner-tier item), or the second failure scenario? I need one more cut named by you, not by me.

**U-13 · Rubric.** The proposal scores itself 87/100 against inferred weights. The judge file says those weights do not exist and warns against planning to them. Confirm I should keep planning against judge clauses only — and that the self-score in Step 5 does not appear anywhere in the repo, README, or video.

**U-14 · Hard-refusal band definition (C-10).** I have proposed: issuer-outage cluster >15, opted-out, already-paid-elsewhere, episode age >72h. Is that the right third band, or should above-₹X also refuse outright rather than escalate?

**U-15 · `where-the-llm-is-not.md` is named in the judge file and in proposal §13, but is missing from the proposal's repo tree in §11.** Confirming I add it — and `BUILD_LOG.md`, which is a judge [F][HARD] and also absent from that tree.

---

**What I need from you before D1 hour 3:** U-02, U-03, U-08, and U-13. The rest can wait until D2 evening without stalling the build.
