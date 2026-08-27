# Build Log

Entries are written at the end of each working session and are never
backfilled. If a session isn't logged the day it happened, it doesn't get
added later — that's the point of this file existing at all.

## D1 — 25 Aug 2026

Phase 0 only: repo skeleton, typed settings, the closed error taxonomy,
structured logging, and the pre-registered `outcome_model.md`. No domain
logic — nothing under `src/gate`, `src/diagnose`, `src/choose`,
`src/execute`, `src/attribute`, or `src/audit` exists yet, by design.

**What I set up:** `src/config.py` (pydantic-settings `Settings`, every
credential field `Optional`, `load_settings()` succeeds with zero env vars
and no `.env`), `src/errors.py` (the closed `SecondRailError` taxonomy with
the expected-control-flow-vs-abort split documented in its module
docstring), `src/logging_setup.py` (JSON logs to stderr, IST timestamps, a
`LoggerAdapter` for run_id/episode_id context), and the Makefile with every
target from the spec — real ones for `setup`/`doctor`/`lint`/`test`/`clean`,
and "not yet built" placeholders that exit 0 for everything downstream of
Phase 0.

**What I assumed:** the attribution window (48h) and the false-positive
goodwill proxy (₹15/contact) are both written into `outcome_model.md` as
named assumptions with their justification, per the phase spec's explicit
default. Neither source document states these numbers, and the blueprint
lists both as open questions (U-08, U-10) — I'm treating the phase-0 prompt's
"use 48 hours unless told otherwise" as the resolution for U-08 specifically,
since it's an explicit instruction rather than a silent guess. U-02 (student
eligibility), U-03 (test-key application status), and U-13 (rubric framing)
are still genuinely open and don't get touched by anything in this phase —
they don't block foundation code, but they block treating this blueprint as
committed to before real build hours go in past D1.

**What didn't work as expected:** two things, both process rather than
logic bugs.

First, I wrote `src/config.py`'s `doctor` command as a single
`@app.command()` on a bare `typer.Typer()`, on the assumption that
`python -m src.config doctor` would just work. It didn't — Typer collapses
an app with exactly one registered command into a single-command CLI, so
`doctor` was parsed as an unexpected positional argument, not a subcommand
name (`Got unexpected extra argument (doctor)`, exit 2). I only caught this
because I ran the acceptance test myself instead of trusting the code as
written. Fix: added an explicit no-op `@app.callback()`, which tells Typer
this is a command group even with one member, so `doctor` resolves as a
named subcommand. Worth remembering for Phase 8's `approve` CLI and any
other single-command Typer entry points later in the build.

Second, this dev machine has no real `make` — only `mingw32-make.exe`
(GNU Make 3.82) under `C:\MinGW\bin`, and Windows' `python3.11` is a
WindowsApps app-execution-alias rather than a normal executable.
`mingw32-make`'s `CreateProcess` call can't resolve that alias the way bash
can, so `mingw32-make setup` failed with "the system cannot find the file
specified" even though `python3.11 --version` works fine directly in bash.
I made the Makefile detect `$(OS)` and switch between `.venv/bin` and
`.venv/Scripts` so it's still correct for a Windows contributor, but I
verified `setup`/`doctor`/`lint`/`test` by running each underlying command
directly against `.venv/Scripts/python.exe` rather than through `make` on
this machine — the Makefile's `python3.11 -m venv` line is unverified by an
actual `make setup` run here and should get a real check on a Linux/Mac box
(or inside a judge's environment) before I lean on it during the demo.
I also briefly copied `mingw32-make.exe` to `C:\MinGW\bin\make.exe` to get a
`make` alias for testing, realized that's a system-wide change nobody asked
for, and deleted it again — noting it here so the git history doesn't have
to explain why a random binary showed up and vanished in a system directory
that isn't part of this repo.

Ruff also flagged `tests/test_config.py`'s import block as unsorted on the
first run, because ruff's isort didn't know `src` was first-party and
grouped it with third-party imports. Added `known-first-party = ["src"]`
to `pyproject.toml`; that's now `make lint`-clean.

## D1 (evening) — 25 Aug 2026

Phase 1: `src/razorpay_client.py` (raw httpx, not the SDK, so the audit
trail can carry the real status code/body/request-id), `scripts/harvest_errors.py`
(the D2 harvest job), and every real doc source it depends on. This session
did not produce `evidence/harvested_errors.jsonl` — see below, it's a hard
blocker, not a shortcut I took.

**What I read before writing anything, per the phase's own instruction not
to invent test data from memory:** the current (fetched today) rendered
versions of Razorpay's test-card page, test-UPI page, the generic API
error-response page, the common-errors page, the Payment entity reference,
and the Payment Links create/cancel reference. The plain-HTTP fetch tool
missed every table on the first two pages — they render client-side — so I
had to load them in an actual browser and read the accessibility tree
instead. Worth remembering for any future doc-scraping step in this
project: don't trust a bare `WebFetch` against razorpay.com/docs to see
tables.

**What I found that I didn't expect:** the blueprint's U-flagged worry about
`error_code`/`error_description`/`error_source`/`error_step`/`error_reason`
possibly having drifted was worth taking seriously, because there are
*two different* Razorpay error shapes that are easy to conflate. The
generic API error-response object (returned when an API *call* itself is
malformed) uses unprefixed fields — `code`, `description`, `field`,
`source`, `step`, `reason`. The Payment *entity's own* fields — what this
project actually reads, via `fetch_payment` or the `payment.failed`
webhook — are the prefixed ones, and I confirmed today they're still on
the live Payments Entity doc page with real examples. Good news for the
architecture; bad news if I'd built `src/diagnose` against the wrong one
without checking.

**Where my first hypothesis was wrong:** I assumed Razorpay would have a
documented, standard-test-account, server-to-server JSON endpoint for
forcing a UPI Collect failure — enough tutorials reference old
`/v1/payments/create/...` S2S flows that I expected `make harvest` to be
fully headless. I spent real time searching (direct URL guesses, the docs
site search box, the site's own error-codes and payments hub pages) and
found no such endpoint documented as of today. What the current docs
actually describe, for both cards and UPI, is: open Razorpay's hosted
checkout, pick a method, enter the test instrument, submit — and for
cards, click Failure on the mock bank screen. There is no way to force a
real failure without completing that page. I designed
`scripts/harvest_errors.py` around that reality instead of the one I
assumed going in: it creates the Payment Links headlessly (real API
calls) and writes a resumable `evidence/harvest_manifest.json`, but
completing each checkout is a separate, explicit interactive pass —
either by me driving a real browser against the real `short_url`s, or by
whoever runs this next.

**The reference_id probe (Step 5) result:** not yet run. Razorpay's own
Payment Links "Create" error list documents `payment link creation with
reference ID already attempted` as a 400 response to a duplicate
`reference_id` — so the *documented* answer is "rejected" — but the phase
spec is explicit that documentation isn't the same as the empirical
answer, and I haven't been able to run the probe for real yet. It's fully
built and headless (`_reference_id_probe` in `scripts/harvest_errors.py`)
and will run automatically the moment `make harvest` has real credentials
to work with.

**The actual blocker:** there is no `.env` in this repo and no Razorpay
test-mode keys anywhere I have access to. Every non-negotiable this
project has committed to — no mocks, no fake data, fail loudly instead of
faking canned data when a key is missing — means I will not synthesize
`harvested_errors.jsonl`. I verified the one thing that *is* verifiable
without keys: `make harvest` reads `.env`, finds nothing, and exits 2 with
`[MISSING_RAZORPAY_CREDENTIALS] missing required Razorpay credential(s):
RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET — copy .env.example to .env and fill
in the missing value(s)` — no traceback, no partial file written to
`evidence/`. That satisfies the phase's "exits 2, never crashes"
requirement on its own, but the >=20-record harvest, the field-existence
report, the error-code snapshot, and the reference_id probe result all
still need a real key before they can be anything but "not yet run."

## D1 (night) — 25 Aug 2026

Real keys arrived and I ran the actual harvest. `evidence/harvested_errors.jsonl`
now has 20 real captured payment objects from live Razorpay test mode —
the >=20 minimum, hit exactly. This entry is the honest account of how
that went, which was not smoothly.

**Before anything else: a credential-handling near-miss.** The first
attempt to add keys put a real-looking secret into `.env.example` — a
file that's tracked in git, not `.env`. I caught it before staging
anything (`git status` showed it modified, nothing committed), reverted
it with `git checkout -- .env.example`, and asked for the credentials to
go into `.env` instead. The second attempt also had a mislabeled
`RAZORPAY_KEY_ID` (literally the string `key_secret`) — I didn't try to
guess which value was which and asked for the dashboard's actual Key ID
instead. Worth a standing rule for this project: never assume a
credential-shaped string is correctly labeled, and never let one sit in a
tracked file even mid-conversation, even if it never gets `git add`ed.

**Which test instruments actually produced failures, and which didn't:**

- **Card — mechanically yes, semantically no.** All 8 documented "Error
  Scenario" card numbers from the test-card docs went through checkout
  and failed when Failure was clicked on the mock bank screen, exactly as
  documented. But every one of them came back with the same generic
  `error_code: BAD_REQUEST_ERROR`, `error_reason: payment_failed`,
  `error_description: "Payment failed"` — not the specific reason each
  card number is supposed to trigger (`insufficient_fund`,
  `card_declined`, `authentication_failed`, etc.). I expected per-card
  specificity and got a single generic gateway-authorization failure
  instead, on 8/8 distinct real card numbers. This is now the headline
  finding in `evidence/razorpay_field_report.md` — the kind of result
  that's supposed to be reported as a headline, not quietly dropped.
- **UPI — not available at all.** The checkout page for every UPI-planned
  scenario offered Card, Netbanking, and Wallet, never UPI, on this
  account. Confirmed directly (asked the person doing the manual checkout
  what tabs were visible), not inferred. No amount of retrying the
  checkout would have fixed this — it's an account-level thing, not a
  per-request one. The 17 UPI-planned scenarios were completed via Card
  instead (reusing the same 8 known numbers), which is why
  `harvested_errors.jsonl` now writes `instrument`/`forced_by` from the
  *real* `payment.method`/card/vpa/bank on each captured payment, with
  the original plan kept alongside as `planned_instrument`/
  `planned_forced_by` — the first version of this script conflated the
  two and would have quietly mislabeled 12 real card payments as "upi."
- **Netbanking — one real data point.** One UPI-planned link ended up
  completed via Netbanking (bank code `CNRB`) before Card became the
  fallback, and it returned a different reason, `payment_cancelled` —
  the only record in the whole batch that isn't `payment_failed`. Not
  enough data to draw a real pattern from, but it's genuine and it's in
  the file.

**Where my first hypothesis was wrong, a second time:** I assumed
Razorpay's rate limiting on `POST /v1/payment_links` was a short,
per-second thing my token bucket could ride out with a lower rate and a
short retry backoff. It wasn't. At 2 rps, 14/26 scenario creations 429'd
even after 4 retries; dropping to 0.5 rps got through 25/26, but the
last link and the `reference_id` probe never recovered even after a
90-second-spaced retry loop ran for 7.5 minutes and then a further ~30
minutes of intermittent manual retries. The actual error body —
`{"error": {"code": "BAD_REQUEST_ERROR", "description": "Too many
requests"}}` — matches Razorpay's own documented generic rate-limit
error, not a distinct "30 Payment Links per business" cap message, and
cancelling an already-used link didn't free capacity to create a new
one, which argues against the lifetime-cap theory and for a
longer-than-expected time window instead — though I can't confirm the
exact mechanism, only that it outlasted every wait I tried today. The
uncomfortable possibility I didn't account for going in: my own retries,
including the bounded backoff inside `RazorpayClient`, likely counted
against whatever window Razorpay tracks, so repeatedly retrying a
429'd endpoint may have been extending my own lockout rather than
riding it out. I stopped retrying once I noticed this rather than keep
digging.

**The `reference_id` probe: attempted, not completed.** Every attempt
this session hit the same 429 on its first `create_payment_link` call.
Razorpay's own docs *state* the answer — `payment link creation with
reference ID already attempted`, HTTP 400 — but this project has been
explicit throughout that a documented answer is not the same as an
empirically confirmed one, and I'm not going to blur that line just
because the probe is inconvenient to finish today. It's fully built,
persists its result the first time it succeeds
(`evidence/harvest_probe_result.json`), and `evidence/razorpay_field_report.md`
says exactly this — attempted with real credentials, still not
answered — rather than the old placeholder that implied no key was
available.

**Also found, positively:** Payment Link responses carry an `order_id`
field that is not in Razorpay's documented response schema for
`POST /v1/payment_links`. That turned out to be the thing that made the
whole harvest practical without asking anyone to hand back payment IDs
by hand — every payment attempt against that order, captured or failed,
is discoverable via `fetch_order_payments`, so the human-in-the-loop step
is just "complete the checkout," nothing more.

**A real-PII miss, caught before committing anything.** The Payment
entity also carries `email` and `contact` — whatever the person
completing checkout typed into the required contact fields. 19 of the 20
checkouts used made-up values, but one used a real phone number, which
means it was sitting in `evidence/harvest_manifest.json` and
`evidence/harvested_errors.jsonl` on disk — files this project commits
to a public repo. `git status` confirmed neither had been staged yet, so
nothing was ever at risk of landing in history, but the files existed
with real PII in them before I checked. Added `_redact_pii` in
`scripts/harvest_errors.py` (redacts `email`, `contact`, and `vpa` to
`"[redacted]"` at the moment a payment is captured, not just before
writing the JSONL — so the manifest never holds it either) and
retroactively scrubbed the two files already on disk. This should have
been part of the original design, not a patch after the fact — anywhere
this project reads back a real customer-facing field from a live
Razorpay object, redaction needs to happen at the capture boundary, by
default, not as something I remember to add when I notice the field
exists.

## D2 — 26 Aug 2026

Phase 2: `src/db/schema.sql`, `migrate.py`, `repo.py`, and
`tests/test_db_constraints.py`. No business logic — `src/gate/`,
`src/diagnose/`, etc. still don't exist. `make migrate`, `make migrate`
again, `make db-check`, and `make test` all run clean via the real
Makefile targets (verified through `mingw32-make`, not just the
underlying Python commands — see the D1 entry on why that distinction
matters on this machine).

**Two arithmetic discrepancies in the phase spec, both resolved by
following the blueprint instead of the summary text, per this project's
own stated tie-break rule:**

1. The phase prompt's header says "Tables (14)" and the acceptance test
   says "→ 14 tables", but its own detailed column-level spec — which
   matches `second-rail-build-blueprint.md` §6 line for line — defines
   16 named tables, including `harvested_error` (M-08's evidence anchor)
   and `exception_entry` (C-05's "no episode silently dropped" guarantee).
   Built all 16. Dropping either of those two to hit "14" would have
   broken a judge-facing clause other phases depend on, for the sake of
   matching a number that appears to be a simple miscount.
2. The VERIFY block expects `PRAGMA index_list(episode)` to show 4
   indexes; it shows 5. `episode_id` is a `TEXT PRIMARY KEY`, not
   `INTEGER PRIMARY KEY` — SQLite only aliases the rowid for the integer
   case, so a text primary key gets its own real auto-index
   (`sqlite_autoindex_episode_1`, `origin: pk`) in addition to the
   auto-index the `UNIQUE(payment_id)` constraint creates
   (`sqlite_autoindex_episode_2`, `origin: u`) and the 3 explicit
   `CREATE INDEX` statements — 3 + 1 + 1 = 5. This is standard SQLite
   behavior for a non-integer PK, not a schema bug, and I didn't reshape
   the schema to force the count down to match a VERIFY line that assumed
   integer-PK aliasing.

Both are flagged here rather than silently "fixed" to match the stated
numbers, since every PK in this schema is deliberately a TEXT id
(`ep_*`, `pay_*`, ULIDs elsewhere) — switching to `INTEGER PRIMARY KEY`
anywhere just to make an index count line up would be a worse schema for
a database whose primary keys are meant to be stable, externally
meaningful identifiers, not autoincrementing local counters.

No real assumption-under-pressure moment this session beyond those two —
the schema, migration idempotency, and constraint tests all matched the
spec cleanly once the table-count question was settled.

## D2 (later) — 26 Aug 2026

Phase 3: `src/audit/writer.py`, `src/audit/verify.py`,
`tests/test_audit_chain.py`. Hash-chained JSONL audit log, streaming
verifier, tamper-test CLI.

**Where my first hypothesis was wrong, and it wasn't in my own logic.**
`make verify-audit` on the real 2000-record file from the acceptance
script printed `chain BROKEN at seq 1000` — reproducibly, on every run.
My first instinct was to distrust `verify_chain()` itself: reread the
hash-chaining math, re-derived the genesis value, checked for an
off-by-one in the streaming lookahead. All of it was correct — calling
`verify_chain()` directly against the exact same file, in a fresh
Python process, reported `intact: True`. The bug wasn't in the function;
it was in how it got *invoked*. Calling `main()` (the Typer callback)
directly as a plain Python function also worked. Only going through the
real CLI — `python -m src.audit.verify --all` — broke it, and it broke
at exactly seq 1000, the halfway point of 2000, every single time.

Isolated it with a five-line reproduction outside this project entirely:
a bare `typer.Typer()` app with one `bool = typer.Option(False, "--all")`
parameter. Called with `--all` on the command line, the parameter came
through as `None`. Called with no flag at all, it came through as the
*string* `'False'`, not the boolean `False`. `requirements.txt` pins
`typer==0.12.5` but never pinned `click` — Typer's own transitive
dependency — so `pip install` resolved the newest available click,
8.4.2, released long after 0.12.5 and apparently carrying a regression
in how boolean `Option` values get coerced. Pinned `click==8.1.7`
(contemporaneous with 0.12.5) in `requirements.txt`, reinstalled, and
the flag started arriving as a real `True`/`False`. `--all` had silently
been landing on the "verify the newest single file with no flags"
default path this whole time, which happened to also be `run_test.jsonl`
— so the two-file aggregation logic was *never actually exercised* by
any of my manual testing until I went looking for why the number was
wrong, not just whether a number appeared.

The lesson worth keeping: when a fast, deterministic, and *exact* wrong
answer shows up (broken at exactly the halfway point, every time,
independent of what's in the file), that's a much stronger signal of a
plumbing/parsing bug upstream of your logic than of a subtle bug in the
logic itself. Real business-logic bugs are rarely that clean. I nearly
spent the debugging budget re-deriving hash arithmetic that was already
correct instead of asking "does this function even see what I think it
sees" first.

**Separately, and expected:** the acceptance script's own snippet
constructs an `AuditWriter` for `run_id="run_test"` without ever calling
`start_run()`, so `audit_record.run_id`'s foreign key to `run(run_id)`
has nothing to point at. Every one of the 2000 mirror-insert attempts
failed with `sqlite3.IntegrityError: FOREIGN KEY constraint failed`,
each one caught, logged as a warning, and correctly *not* rolled back —
the JSONL file still has all 2000 records, still verifies intact, none
of the writes were lost. This is the append-only discipline working
exactly as specified ("the JSONL line has already been written — that
is correct and intentional"), not a bug — real callers create the `run`
row first. Left as-is rather than special-cased for the smoke-test
script, since papering over a missing `start_run()` call would hide the
same mistake in a real caller too.

`make verify-audit` timing on the acceptance run: `chain intact — 2000
records (0.08s)`, comfortably under the 2s budget. Writing those 2000
records (fsync on every line, by design) took several seconds on this
machine's OneDrive-synced working directory — worth knowing about for
the demo, since a slow disk under fsync pressure is a real, visible
thing on camera, but it's the write path, not the verify path the phase
actually budgets.

## D2 (evening) — 26 Aug 2026

Phase 4, first commit: `config/taxonomy.yaml`, provisional. Nine classes,
matching the shape `outcome_model.md` §2 already pre-committed to before
this phase started. Anchors at this stage cite
`evidence/razorpay_error_codes_snapshot.md` only (`source: doc`) — no
`harvest_id`, deliberately, since the point of this commit is to fix the
taxonomy's *shape* (field structure, class count, the regex/anchor
convention) before the next commit cross-checks every anchor string
against the real harvest file field-by-field. `make config-check` is
expected to fail against this version — that's the intended signal that
ratification hasn't happened yet, not a bug in the checker.

Phase 4, second commit: ratified `config/taxonomy.yaml`, plus
`config/policy_table.yaml`, `config/guardrails.yaml`,
`src/config_models.py`, `scripts/config_check.py`,
`docs/where-the-llm-is-not.md`, `tests/test_config_artifacts.py`, and the
`make config-check` Makefile target.

**The anchoring problem, and the decision it forced.** Re-reading
`evidence/harvested_errors.jsonl` field-by-field before writing a single
anchor: 19 of the 20 records share the exact same `error_code`
(`BAD_REQUEST_ERROR`) and `error_description` (`"Payment failed"`) —
`evidence/razorpay_field_report.md`'s own D1-night headline finding, that
8 distinct real test-card numbers all came back through the gateway with
one generic envelope. Anchoring a nine-class taxonomy on
`error_code`/`error_description` alone, as the phase template's YAML
example implies, would either produce two real classes (generic-card and
the one genuinely-differentiated netbanking-cancellation record) or
require pretending 18 generic strings are more distinguishable than they
are. Neither is honest. The field that *does* carry real per-record
signal, verbatim in the same file, is `planned_error_reason` — what the
harvest harness told each specific payment to force, using Razorpay's own
documented reason vocabulary (cross-referenced in
`evidence/razorpay_error_codes_snapshot.md`). Every anchor in the ratified
file carries `error_code`, `error_description`, *and* `reason` — all
three checked verbatim against the sourced harvest record by
`make config-check` check 2 — rather than silently anchoring on the field
that doesn't actually distinguish anything and hoping nobody checks.

**Nine classes, derived, not assumed.** All 20 harvested records sorted
cleanly into 9 classes by their `planned_error_reason` with no leftover
and no `source: inferred` class needed — insufficient_funds,
issuer_declined, invalid_entered_details, authentication_failed,
limit_or_attempts_exceeded, customer_abandoned, payment_timed_out,
issuer_bank_technical_error, device_or_app_unreachable. This matches the
nine-class shape `outcome_model.md` §2 pre-committed to before this phase
started, but the count wasn't chosen to hit nine — it fell out of
grouping 18 distinct `planned_error_reason` values by shared policy
implication (does the same instrument retry make sense, or not) and
landed on nine on its own.

**Design consequence flagged for Phase 5+, not buried:** a regex baseline
classifier that reads only `error_description` will misclassify nearly
every card-instrument episode in this evidence set into one bucket, since
that field doesn't vary. `regex_patterns` in the ratified taxonomy match
both the raw reason token and the documented human-readable phrasing, so
the baseline has a chance against a real payload exposing either form,
but this is exactly the kind of result the project's non-negotiables ask
to report as a headline (regex vs. LLM, §"non-circular metrics") rather
than something Phase 5 discovers cold.

**Policy table:** 27 explicit rules (3 per cause class, varying
band/segment/instrument) plus a conservative `default_rule`
(`[open_ticket, no_action]`, `human_keystroke`) resolving the remaining
297 of 324 cartesian cells — `make config-check` check 4 confirms all 324
resolve. Tier logic is one stated rule applied consistently: band A1 is
auto, A2 is auto for repeat/high_value and human_keystroke for
first_time, A3 is human_keystroke always — A3's floor (500,001 paise)
sits exactly one paise above `guardrails.auto_approve_ceiling_paise`
(500,000), which is the literal mechanism behind "human keystroke above a
rupee threshold." None of the nine classes is itself non-recoverable, so
`hard_refuse` in this design comes entirely from the four runtime
`hard_refuse_conditions` (opt-out, already-paid, episode-age cap,
issuer-outage cluster), not from a class-level flag — Phase 7 wires the
actual checks.

**Verification:** `make config-check` (run directly as
`python -m scripts.config_check` — no `make` binary on this machine, same
as every prior phase) prints all 8 PASS lines; `wc -l
config/guardrails.yaml` is 16; `ruff check` is clean; all 32 tests pass
(6 pre-existing suites plus the 6 new cases in
`tests/test_config_artifacts.py`).

## D2 (night) — 26 Aug 2026

Phase 5: `data/generator.py`, `scripts/seal.py`, `scripts/holdout_guard.py`,
`tests/test_generator.py`, `tests/test_holdout_guard.py`, plus an
`outcome_model.md` §2 appendix and `generation_weight` /
`response_base_rate` fields on every `config/taxonomy.yaml` class (own
commit, since neither the cause mix nor the sealed labels' response draw
can run without concrete numbers, and the amendment policy says corrections
go in a dated appendix, not a silent edit).

**Where the first attempt was wrong.** The phase brief's own acceptance
test checks "at least 10 distinct `error_description` values in sealed
absent from train." Wrote the generator against that literally first, then
ran it against the real 20-record harvest file and got nowhere near 10 —
because Phase 4's own ratified `config/taxonomy.yaml` header already
established that 19 of those 20 records share one generic
`error_description` ("Payment failed"). Only two distinct description
strings exist in the whole harvest set; no partition of it can produce 10
sealed-only values on that field, regardless of how the generator is
written. The field that actually identifies a distinct verbatim record is
`harvest_id` — that's what `SEALED_ONLY_HARVEST_IDS` reserves 11 of (10
required, 11 picked for margin) and what the test and `holdout/SHIFT.md`
check against instead. Flagged as a deliberate deviation in the commit
message rather than silently swapping the field and hoping nobody asks —
this is the same finding from Phase 4's anchoring problem showing up again
in a different acceptance test.

**A second false positive worth naming, not fixing.** The DoD's own
suggested PII check — `grep -rnE "\+?[0-9]{10}" data/ holdout/` — matches
`customers.jsonl` even though the file has no phone numbers, because a
sha256 hex digest is 64 characters of `[0-9a-f]` and a 10-digit run inside
one is common by chance, not a leak. `contact_hash` and `email_hash` are
exactly what DPDP compliance requires here (no raw contact info stored at
all), so the fix is not to change the hash format — it's to check the
right thing. `tests/test_generator.py` asserts no phone/email pattern
appears in any *non-hash* field, and separately asserts the hash fields
really are 64-char hex, not a phone number that happens to satisfy the
same regex.

The other three DoD items were mechanical once the harvest_id decision was
made: `TRAIN_ISSUER_FAMILIES` (6) vs. `BANK_E` (sealed-only, weighted at
20% of sealed episodes so it's reliably present, not just probable); the
sealed lognormal median shifted +20% over train's (both drawn from
`random.Random.lognormvariate`, no numpy); and the ten seeded edge cases,
each built as one (or, for `issuer_outage_cluster` and
`frequency_cap_trip`, many) explicitly-constructed episode rather than
hoped for from the random draw, so the count table is exact every run, not
just likely.

Verified: `make data && make seal && make verify-seal` all exit 0 and
print the one-line "sealed split verified — 200 episodes, sha256:…"
`make data` run twice produces byte-identical `holdout/sealed.jsonl`
(same sha256 both times). Edge-case count table: nine cases at 1
(`frequency_cap_trip` at 3), `issuer_outage_cluster` at exactly 40.
`grep -rn "held-out test set" .` returns nothing outside
`second-rail-build-blueprint.md`, which quotes the judge's original
critique verbatim and predates this phase — left untouched rather than
scrubbed, since it's the historical record of the problem being solved,
not a claim this codebase makes about itself. `ruff check` clean; all 49
tests pass (32 pre-existing plus 17 new across the two Phase 5 suites).

## D3 — 26 Aug 2026

Phase 6: `src/ingest/{signature,normalize,service,app}.py`,
`tests/test_ingest.py`, `fixtures/webhooks/*.json`,
`scripts/replay_webhooks.py`, plus small additions to already-built modules
— `src/db/repo.get_webhook_event`, `src/config.require_webhook_secret`,
and widening `AuditWriter.__init__`'s `run_id` to `str | None`.

**Where the first assumption was wrong.** Went in planning to read the
webhook's own dedup id off the JSON body — every other identifier in this
project (`payment_id`, `order_id`) lives in the body, so that felt like the
obvious place to look. Before writing `service.py`'s dedup check, checked
Razorpay's actual webhook docs instead of trusting that assumption (the
proposal itself flagged its own recall of Razorpay specifics as
unreliable), and the real answer is different: the payload has no event
id of its own at all. Razorpay puts it in the `X-Razorpay-Event-Id`
*header*, explicitly documented as the field to dedup on, separate from
`X-Razorpay-Signature`. That's a real design fork, not a naming detail —
it meant `IngestService.handle_event` takes `event_id` as a caller-supplied
argument rather than extracting it from `payload`, and it settled a second
question for free: since a retried delivery of the *same* event reuses the
same header value, and `webhook_event.event_id` is that table's PRIMARY
KEY (fixed back in Phase 2's schema), a literal replay can never produce a
second row — it can only ever be caught by a SELECT-before-INSERT check
and recorded via the audit log, not via a second `dedup_result="duplicate"`
row. The *other* duplicate path — same `payment_id`, different
`event_id` — is what actually produces that second row, via
`payment_failed_duplicate.json`. `tests/test_ingest.py` encodes both paths
as separate cases rather than only the one the phase brief's literal DoD
wording implied.

**Second thing that needed a real decision, not a default.** The webhook
server is a long-lived process, not a batch `run` — but `audit_record.run_id`
is a foreign key into `run`, whose `mode` enum (`dry_run`/`execute`/`fixture`)
has no slot for "serving." Rather than invent a `run` row for a process that
isn't a run, or bypass the FK with a bogus id, `AuditWriter` now accepts
`run_id: str | None`; the ingest server passes `None` and gets its own
append-only `evidence/audit/ingest.jsonl`, distinct from any batch's
`{run_id}.jsonl`. `NULL` on a FK column is never checked by SQLite, so this
costs nothing and is exactly the honest answer: these audit records aren't
part of a run.

Also hit, and worth naming: `sqlite3.Connection` objects are single-thread
by default, and the background worker thread that actually processes
queued webhooks is not the thread that opened the connection used by
`GET /health`. First version shared one connection across both and every
queued event failed with `SQLite objects created in a thread can only be
used in that same thread` — silently, since the worker only logs and moves
on rather than crashing the server (that's deliberate: one bad event must
never take down the listener). Fixed by giving the worker thread its own
connection, `AuditWriter`, and `IngestService`, opened inside the thread
function itself rather than passed in from the main thread.

Verified end to end on a real local run (`make serve` had no `make` on
this Windows/git-bash shell, so ran the equivalent `python -m uvicorn …`
directly): `GET /health` → `{"status":"ok","run_id":null,"db":"ok"}`;
`scripts/replay_webhooks.py` against all 6 fixtures →
`webhook_event` dedup_result counts `new: 2, duplicate: 1, out_of_order: 3`;
`episode` count `1` (only `payment_failed.json`; the malformed fixture
correctly produced zero episodes and one `exception_entry` row with
`reason_code="SCHEMA_DRIFT_FIELD_MISSING"` instead); `make verify-audit`
equivalent → `chain intact — 2006 records`. `ruff check` clean; all 56
tests pass (49 pre-existing plus 7 new in `test_ingest.py`).

## D4 — 27 Aug 2026

Phase 7: the deterministic gate. `src/gate/{checks,engine,stopping}.py`,
`src/runner.py` (the batch orchestrator every later phase plugs into),
`tests/test_gate.py`, `tests/test_llm_boundary.py`, plus small additions
to `src/db/repo.py` (`insert_customer_if_absent`,
`get_opted_out_customer_ids`). `make gate-run` processes 600 episodes with
zero LLM calls and zero network calls, per the phase's own hard rule —
`test_llm_boundary.py` enforces it by grepping `src/gate/`, `src/audit/`,
`src/ingest/`, `src/db/` (and `src/execute/`/`src/attribute/` once they
exist) for the LLM client symbol and `"openai"|"genai"|"anthropic"`.

**Where the first hypothesis was wrong — the bigger one.** First version
of `Runner.run()` used a single fixed instant (`data.generator.REFERENCE_NOW`,
the generator's own reference timestamp) as "now" for every episode in a
600-episode batch, on the assumption that a byte-identical, reproducible
batch replay needed a byte-identical clock. Ran it, and the tier
breakdown came back `{'hard_refuse': 47}` — no `auto`, no
`human_keystroke` at all, which fails the phase's own acceptance check
that all three escalation tiers appear in the audit log. The cause wasn't
a bug in `check_episode_age` — it was a bad assumption about what "now"
should mean for a batch. `data/generator.py` draws each episode's
`failed_at` uniformly across a 30-day window; measuring every one of them
against a single instant near the *end* of that window meant ~90% of the
batch was "more than 72 hours old" by construction, regardless of
processing order — the age cap was firing on almost everything, for a
reason that had nothing to do with the cap actually doing its job. The
seeded data only intends *one* episode to ever trip that check
(`episode_older_than_window`, where `received_at` is deliberately pushed
far past `failed_at`) — for every other episode, `received_at` is minutes
after `failed_at`, modelling a system that gates an episode shortly after
it arrives. So that's what "now" should default to per episode:
`episode.received_at`, not one global instant. An explicit `now` still
overrides this globally, which is what every unit test in
`tests/test_gate.py` uses to get a fixed, freezegun-friendly clock — only
the CLI's real-batch default changed. Re-ran after the fix:
`{'auto': 1, 'human_keystroke': 2, 'hard_refuse': 44}` — all three tiers
present, as they should be.

**Second wrong assumption, smaller, caught before it shipped rather than
after.** The phase brief says to key cluster detection on `(error_code,
issuer_family)`. Checked the actual seeded 40-episode outage cluster in
`data/train.jsonl` before wiring that up, and neither half of that key
works: `error_code` is the literal string `"BAD_REQUEST_ERROR"` on *every*
one of the 600 episodes in this dataset (Razorpay's gateway collapses
everything to one HTTP-level code), so it carries zero signal; and
`issuer_family` is deliberately *randomised* per member of the seeded
cluster in `data/generator.py` (Phase 5) — modelling one upstream gateway
fault surfacing at several different banks in the same half hour, which
reads as more realistic than one bank alone. Keying on `(error_code,
issuer_family)` as written would fragment the one real 40-episode cluster
into six sub-groups of 4-9 across issuer families, none over
`outage_cluster_threshold` (15) — the seeded scenario would never
collapse to a single refusal, silently. Switched the key to
`error_reason` alone, which actually is constant across the seeded
cluster (`"gateway_technical_error"`, checked empirically against the
full 600-episode set — 76 total episodes share it, only 40 of which are
the seeded cluster, and the other 36 are spread across the full 30-day
window, not concentrated enough to false-trigger the same 30-minute-window
threshold). Documented the reasoning in `src/gate/engine.py`'s module
docstring rather than just silently deviating from the brief's literal
wording.

Verified end to end: `python -m src.runner --gate-only` over the real 600
episodes → `episode_count=600 == actioned(0) + suppressed(45) +
execution_failed(0) + pending(555)`; escalation tiers `auto=1,
human_keystroke=2, hard_refuse=44`; exactly one `stage=stop` audit record,
rationale naming `shared_cause_cluster` and the 40-episode count;
`stopped_reason=cluster_escalation`. Separately, `touch KILL` before the
same command → zero episodes processed, `stopped_reason=kill_switch`,
confirming the kill switch is checked before the first episode, not after.
`make verify-audit` equivalent → `chain intact — 48 records`. `ruff check`
clean; all 70 tests pass (56 pre-existing plus 14 new across
`test_gate.py` and `test_llm_boundary.py`). `make` itself still isn't on
this Windows/git-bash shell (same as Phase 6) — ran the `python -m
src.runner` command the Makefile's `gate-run` target wraps, directly.
