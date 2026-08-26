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
