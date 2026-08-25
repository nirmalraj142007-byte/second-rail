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
